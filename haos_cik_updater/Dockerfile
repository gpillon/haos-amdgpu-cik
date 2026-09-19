ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN apk add --no-cache glib python3

COPY rootfs /

RUN chmod 0755 /run.sh

CMD ["/run.sh"]
