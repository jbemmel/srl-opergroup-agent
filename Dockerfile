ARG SR_LINUX_RELEASE
FROM srl/custombase:$SR_LINUX_RELEASE
# FROM ghcr.io/nokia/srlinux:$SR_LINUX_RELEASE
# FROM registry.srlinux.dev/pub/srlinux:$SR_LINUX_RELEASE

# admin user doesn't exist yet
ARG SSH_KEY
RUN sudo mkdir -p /home/admin/.ssh && \
    sudo echo "$SSH_KEY" > /home/admin/.ssh/authorized_keys && \
    sudo chmod 700 /home/admin/.ssh && \
    sudo chmod 600 /home/admin/.ssh/authorized_keys

# Install pyGNMI to /usr/local/lib[64]/python3.6/site-packages
RUN sudo yum install -y python3-pip gcc-c++ socat && \
    sudo python3 -m pip install pip --upgrade && \
    sudo python3 -m pip install pygnmi sre_yield

# This does not work; use socat forwarding instead
# RUN sudo sed -i 's|launch-command:  ./sr_sdk_mgr|launch-command:  /usr/sbin/ip netns exec srbase-mgmt ./sr_sdk_mgr|g' /opt/srlinux/appmgr/sr_sdk_mgr_config.yml

# --chown=srlinux:srlinux
RUN sudo mkdir -p /etc/opt/srlinux/appmgr/
COPY ./appmgr/ /etc/opt/srlinux/appmgr

# COPY ./appmgr/ /home/appmgr
# RUN sudo mkdir -p /etc/opt/srlinux/appmgr/ && sudo cp /home/appmgr/* /etc/opt/srlinux/appmgr/

# Using a build arg to set the release tag, set a default for running docker build manually
ARG SRL_AUTO_CONFIG_RELEASE="[custom build]"
ENV SRL_AUTO_CONFIG_RELEASE=$SRL_AUTO_CONFIG_RELEASE
