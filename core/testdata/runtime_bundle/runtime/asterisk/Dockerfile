FROM debian:12-slim

ARG ASTERISK_VERSION=20.19.0
ARG ASTERISK_SOURCE_URL=https://downloads.asterisk.org/pub/telephony/asterisk/old-releases/asterisk-20.19.0.tar.gz
ARG ASTERISK_SOURCE_SHA256=10fbc070f9aaec50b788962dbcddf694fcc61d49c1f1bb158bf04a2e7e46aba0
ARG CISCO_PATCH_URL=https://raw.githubusercontent.com/usecallmanagernz/patches/master/asterisk/cisco-usecallmanager-20.19.0.patch
ARG CISCO_PATCH_SHA256=adcb88c17a1cf8eb6242c7d9f1959f06afc64ae9af210268abaf767d388b83d2

LABEL org.opencontainers.image.title="PBX Asterisk 20 Cisco USECALLMANAGER"
LABEL org.opencontainers.image.version="20.19.0-cisco"
LABEL org.opencontainers.image.source="https://usecallmanager.nz/patching-asterisk.html"

ENV DEBIAN_FRONTEND=noninteractive
SHELL ["/bin/sh", "-euxc"]

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
    autoconf \
    automake \
    bison \
    build-essential \
    ca-certificates \
    curl \
    file \
    flex \
    gettext \
    libasound2-dev \
    libcurl4-openssl-dev \
    libedit-dev \
    libjansson-dev \
    libncurses-dev \
    libogg-dev \
    libopus-dev \
    libsrtp2-dev \
    libsqlite3-dev \
    libspeexdsp-dev \
    libssl-dev \
    libtool \
    libvorbis-dev \
    libxml2-dev \
    patch \
    perl \
    pkg-config \
    python3 \
    unixodbc-dev \
    uuid-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /usr/src

RUN curl -fsSLo asterisk.tar.gz "$ASTERISK_SOURCE_URL" \
 && echo "$ASTERISK_SOURCE_SHA256  asterisk.tar.gz" | sha256sum -c - \
 && tar -xzf asterisk.tar.gz \
 && curl -fsSLo cisco-usecallmanager.patch "$CISCO_PATCH_URL" \
 && echo "$CISCO_PATCH_SHA256  cisco-usecallmanager.patch" | sha256sum -c - \
 && cd "asterisk-$ASTERISK_VERSION" \
 && patch -p1 < ../cisco-usecallmanager.patch \
 && ./configure --prefix=/usr --sysconfdir=/etc --localstatedir=/var --with-pjproject-bundled \
 && make menuselect.makeopts \
 && ./menuselect/menuselect --enable chan_sip menuselect.makeopts \
 && make -j"$(nproc)" \
 && make install \
 && make samples \
 && cd /usr/src \
 && rm -rf "asterisk-$ASTERISK_VERSION" asterisk.tar.gz cisco-usecallmanager.patch

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN chmod 755 /usr/local/bin/docker-entrypoint.sh \
 && mkdir -p /etc/asterisk /var/lib/asterisk /var/log/asterisk /var/spool/asterisk /var/run/asterisk

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["asterisk", "-f", "-U", "asterisk", "-G", "asterisk", "-vvv"]
