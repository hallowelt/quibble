FROM debian:jessie

RUN apt-get update && apt-get install -y \
    python3 \
    python3-dev \
    python-tox

RUN apt-get install -y python3-pip git python3-paramiko
RUN mkdir -p /srv/git/mediawiki && \
    mkdir -p /srv/deployment/integration && \
    git clone --bare "https://gerrit.wikimedia.org/r/p/mediawiki/core" "/srv/git/mediawiki/core.git" && \
    git clone --bare "https://gerrit.wikimedia.org/r/p/mediawiki/vendor" "/srv/git/mediawiki/vendor.git" && \
    git clone "https://gerrit.wikimedia.org/r/p/integration/jenkins" "/srv/deployment/integration/slave-scripts"

RUN apt-get install -y mysql-server php5 php5-mysql && \
    /usr/bin/service mysql start

COPY . /opt/quibble
