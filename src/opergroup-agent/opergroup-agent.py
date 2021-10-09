#!/usr/bin/env python3
# coding=utf-8

import grpc
import time
from datetime import datetime
import sys
import logging
import socket
import os
import json
import signal
import traceback
import re
import sre_yield
from concurrent.futures import ThreadPoolExecutor

if 'SRL_IS_INTERACTIVE' in os.environ: # running on SR Linux box?
   SDK_SERVER='unix:///opt/srlinux/var/run/sr_sdk_service_manager'
   GNMI_SERVER='unix:///opt/srlinux/var/run/sr_gnmi_server'
   LOG_PATH='/var/log/srlinux/stdout'
else:
   # to get SDK: docker cp clab-opergroup-lab2-spine1:/usr/lib/python3.6/site-packages/sdk_protos/ .
   # or using sshfs: sudo sshfs -o allow_other,default_permissions,IdentityFile=/home/jeroen/.ssh/id_rsa.pub  \
   #    admin@clab-opergroup-lab2-spine1:/usr/lib/python3.6/site-packages/sdk_protos /mnt/sdk_protos
   sys.path.append('/home/jeroen/srlinux/python/virtual-env/lib/python3.6/site-packages/sdk_protos')

   # This requires the SDK Unix socket to be exposed on the host
   # useradd -m --uid 1002 -s /bin/bash srlinux (for permissions)
   # SDK_SERVER='unix:///tmp/spine1/sr_sdk_service_manager:50053='
   SDK_SERVER=GNMI_SERVER='clab-opergroup-lab-spine1'
   LOG_PATH='/tmp/srlinux_log'

import sdk_service_pb2
import sdk_service_pb2_grpc
import config_service_pb2

# To report state back
import telemetry_service_pb2
import telemetry_service_pb2_grpc

from pygnmi.client import gNMIclient, telemetryParser

from logging.handlers import RotatingFileHandler

############################################################
## Agent will start with this name
############################################################
agent_name='opergroup_agent'

############################################################
## Open a GRPC channel to connect to sdk_mgr on the dut
## sdk_mgr will be listening on 50053
############################################################
channel = grpc.insecure_channel( f'{SDK_SERVER}:50053' )

# channel = grpc.insecure_channel('127.0.0.1:50053')
metadata = [('agent_name', agent_name)]
stub = sdk_service_pb2_grpc.SdkMgrServiceStub(channel)

############################################################
## Subscribe to required event
## This proc handles subscription of: Interface, LLDP,
##                      Route, Network Instance, Config
############################################################
def Subscribe(stream_id, option):
    op = sdk_service_pb2.NotificationRegisterRequest.AddSubscription
    if option == 'cfg':
        entry = config_service_pb2.ConfigSubscriptionRequest()
        # entry.key.js_path = '.' + agent_name + ".*" # filter out .commit.end notifications
        # entry.key.js_path = ".opergroup_agent.oper_group"
        request = sdk_service_pb2.NotificationRegisterRequest(op=op, stream_id=stream_id, config=entry)

    subscription_response = stub.NotificationRegister(request=request, metadata=metadata)
    print('Status of subscription response for {}:: {}'.format(option, subscription_response.status))

############################################################
## Subscribe to all the events that Agent needs
############################################################
def Subscribe_Notifications(stream_id):
    '''
    Agent will receive notifications to what is subscribed here.
    '''
    if not stream_id:
        logging.info("Stream ID not sent.")
        return False

    # Subscribe to config changes, first
    Subscribe(stream_id, 'cfg')

############################################################
## Function to populate state of agent config
## using telemetry -- add/update info from state
############################################################
def Add_Telemetry(js_path, js_data):
    telemetry_stub = telemetry_service_pb2_grpc.SdkMgrTelemetryServiceStub(channel)
    telemetry_update_request = telemetry_service_pb2.TelemetryUpdateRequest()
    telemetry_info = telemetry_update_request.state.add()
    telemetry_info.key.js_path = js_path
    telemetry_info.data.json_content = js_data
    logging.info(f"Telemetry_Update_Request :: {telemetry_update_request}")
    telemetry_response = telemetry_stub.TelemetryAddOrUpdate(request=telemetry_update_request, metadata=metadata)
    return telemetry_response

############################################################
## Function to populate state fields of the agent
## It updates command: info from state auto-config-agent
############################################################
def Update_OperGroup_State(groupname,timestamp,val,targets,is_up):
    js_path = '.' + agent_name + '.oper_group{.name=="' + groupname + '"}'
    value = { "current_states" : { "value": val },
              "last_change" : { "value": timestamp },
              "targets": { "value": ','.join(targets) },
              "target_count": len(targets),
              "group_is_up": is_up }
    response = Add_Telemetry( js_path=js_path, js_data=json.dumps(value) )
    logging.info(f"Telemetry_Update_Response :: {response}")

##################################################################
## Proc to process the config Notifications received by auto_config_agent
## At present processing config from js_path = .fib-agent
##################################################################
def Handle_Notification(obj,groups):
    if obj.HasField('config'):
        logging.info(f"GOT CONFIG :: {obj.config.key.js_path}")
        if agent_name in obj.config.key.js_path:
            logging.info(f"Got config for agent, now will handle it :: \n{obj.config}\
                            Operation :: {obj.config.op}\nData :: {obj.config.data.json}")
            if obj.config.op == 2:
                logging.info(f"Delete opergroup-agent cli scenario, TODO")
                # if file_name != None:
                #    Update_Result(file_name, action='delete')
                response=stub.AgentUnRegister(request=sdk_service_pb2.AgentRegistrationRequest(), metadata=metadata)
                logging.info('Handle_Config: Unregister response:: {}'.format(response))
            else:
                json_acceptable_string = obj.config.data.json.replace("'", "\"")
                data = json.loads(json_acceptable_string)
                if 'oper_group' in data:
                    oper_group = data['oper_group']
                    oper_group['name'] = obj.config.key.keys[0]
                    logging.info(f"Got operational group :: {oper_group}")
                    groups.append( oper_group )

                return True
        elif obj.config.key.js_path == ".commit.end" and groups!=[]:
            logging.info(f"Got commit, starting monitoring thread: {groups}")
            executor = ThreadPoolExecutor(max_workers=1)
            executor.submit(Gnmi_subscribe_changes,groups)
            # Gnmi_subscribe_changes( groups )
    else:
        logging.info(f"Unexpected notification : {obj}")

    return False

def Gnmi_subscribe_changes(oper_groups):
    logging.info(f"Gnmi_subscribe_changes :: {oper_groups}")

    # Assumes group names are unique, could be enforced in YAML model
    # aliases = [ (path,f"#{g['name']}_{i}") for g in oper_groups
    #            for (i,path) in enumerate(list(sre_yield.AllStrings(g['monitor']['value'])))  ]
    monitor_map = { path:g for g in oper_groups
                    for path in list(sre_yield.AllStrings(g['monitor']['value']))  }
    # Could expand g['targets'] = list(sre_yield.AllStrings(g['group']['value']))
    subscribe = {
            'subscription': [
                {
                    'path': path,
                    'mode': 'on_change',
                } for path in monitor_map.keys()
            ],
            'use_aliases': False, # not supported?
            'mode': 'stream',
            'encoding': 'json'
        }
    logging.info(f"gNMI subscribe :: {subscribe}")

    # with Namespace('/var/run/netns/srbase-mgmt', 'net'):
    with gNMIclient(target=(GNMI_SERVER,57400),
    # Need to run in mgmt namespace for this
    # with gNMIclient(target=('127.0.0.1',57400),
                            username="admin",password="admin",
                            insecure=True) as c:
      # c.subscribe(aliases=aliases) not supported?
      telemetry_stream = c.subscribe(subscribe=subscribe)
      for m in telemetry_stream:
        try:
          if m.HasField('update'): # both update and delete events
              parsed = telemetryParser(m)
              logging.info(f"gNMI change event :: {parsed}")
              update = parsed['update']
              if update['update']:
                 for p in update['update']:
                    path = '/' + p['path'] # pygnmi strips first /
                    logging.info(f"Check gNMI change event :: {path}")
                    if path in monitor_map:
                        g = monitor_map[path]
                        if 'states' in g:
                            g['states'][ path ] = p['val']
                        else:
                            g['states'] = { path: p['val'] }
                        logging.info(f"Updated states :: {g['states']}")
                        threshold = g['threshold'][10:]
                        targets = list(sre_yield.AllStrings(g['target_path']['value']))

                        down = sum(s == "down" for s in g['states'].values())
                        if threshold=="any":
                            is_up = down == 0
                        elif threshold=="all":
                            is_up = down < len(g['states'])
                        else:
                            is_up = down < int(threshold)

                        _ts = datetime.fromtimestamp(update['timestamp']/1000000000) # ns -> seconds
                        _timestamp = _ts.strftime("%Y-%m-%d %H:%M:%S UTC")

                        Update_OperGroup_State( g['name'], _timestamp,
                          str(g['states']), targets, is_up )

                        mappings = { k.lower():v for m in g['mapping']['value'].split(',') for k,v in m.split('=') }
                        logging.info( f"Mappings: {mappings}" )
                        if 'is_up' not in g or is_up!=g['is_up']:
                           g['is_up'] = is_up
                           updates = []
                           path_x = path.replace('[','(').replace(']',')')
                           for d in targets:
                               ps = d.split('/')
                               root = '/'.join( ps[:-1] )
                               leaf = ps[-1]
                               val = {
                                 leaf: (mappings['up'] if is_up and 'up' in mappings
                                        else mappings['down'] if 'down' in mappings
                                        else "disable"),
                                 "description": f"Controlled by oper-group {g['name']} last change at {_timestamp}"
                               }
                               logging.info(f"SET gNMI data :: {root}={val}")
                               updates.append( (root,val) )

                           try:
                               c.set( encoding='json_ietf', update=updates )
                           except Exception as rpc_e:
                              logging.error(rpc_e)
                              _o = rpc_e.__context__ # pygnmi wraps this
                              # May happen during system startup, retry once
                              if _o.code() == grpc.StatusCode.FAILED_PRECONDITION:
                                  logging.info("Exception during startup? Retry in 5s...")
                                  time.sleep( 5 )
                                  c.set( encoding='json_ietf', update=updates )
                                  logging.info("OK, success")

        except Exception as e:
          traceback_str = ''.join(traceback.format_tb(e.__traceback__))
          logging.error(f'Exception caught in gNMI :: {e} m={m} stack:{traceback_str} type:{type(e)}')
    logging.info("Leaving gNMI event loop")

##################################################################################################
## This is the main proc where all processing for auto_config_agent starts.
## Agent registration, notification registration, Subscrition to notifications.
## Waits on the subscribed Notifications and once any config is received, handles that config
## If there are critical errors, Unregisters the fib_agent gracefully.
##################################################################################################
def Run():
    sub_stub = sdk_service_pb2_grpc.SdkNotificationServiceStub(channel)

    response = stub.AgentRegister(request=sdk_service_pb2.AgentRegistrationRequest(), metadata=metadata)
    logging.info(f"Registration response : {response.status}")

    request=sdk_service_pb2.NotificationRegisterRequest(op=sdk_service_pb2.NotificationRegisterRequest.Create)
    create_subscription_response = stub.NotificationRegister(request=request, metadata=metadata)
    stream_id = create_subscription_response.stream_id
    logging.info(f"Create subscription response received. stream_id : {stream_id}")

    Subscribe_Notifications(stream_id)

    stream_request = sdk_service_pb2.NotificationStreamRequest(stream_id=stream_id)
    stream_response = sub_stub.NotificationStream(stream_request, metadata=metadata)
    monitoring_groups = []
    try:
        for r in stream_response:
            logging.info(f"NOTIFICATION:: \n{r.notification}")
            for obj in r.notification:
                Handle_Notification(obj,monitoring_groups)
            # TODO clear after every batch?
            # monitoring_groups = []

    finally:
        Exit_Gracefully(0,0)
    return True
############################################################
## Gracefully handle SIGTERM signal
## When called, will unregister Agent and gracefully exit
############################################################
def Exit_Gracefully(signum, frame):
    logging.info( f"Caught signal :: {signum}\n will unregister opergroup agent" )
    try:
        response=stub.AgentUnRegister(request=sdk_service_pb2.AgentRegistrationRequest(), metadata=metadata)
        logging.info( f'Exit_Gracefully: AgentUnRegister response={response}' )
    finally:
        sys.exit()

##################################################################################################
## Main from where the Agent starts
## Log file is written to: /var/log/srlinux/stdout/opergroup_agent.log
## Signals handled for graceful exit: SIGTERM
##################################################################################################
if __name__ == '__main__':
    # hostname = socket.gethostname()
    signal.signal(signal.SIGTERM, Exit_Gracefully)
    if not os.path.exists(LOG_PATH):
        os.makedirs(LOG_PATH, exist_ok=True)
    log_filename = f'{LOG_PATH}/{agent_name}.log'
    logging.basicConfig(filename=log_filename, filemode='a',\
                        format='%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s',\
                        datefmt='%H:%M:%S', level=logging.INFO)
    handler = RotatingFileHandler(log_filename, maxBytes=3000000,backupCount=5)
    logging.getLogger().addHandler(handler)
    logging.info( f"Starting opergroup agent env={os.environ}" )
    Run()