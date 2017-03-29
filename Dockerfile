FROM alpine:3.5
MAINTAINER Ilya Stepanov <dev@ilyastepanov.com>

ENV GOPATH /go
ENV PYTHONUNBUFFERED 1

RUN apk add --no-cache python3 docker openssl && \
    python3 -m ensurepip && \
    rm -r /usr/lib/python*/ensurepip && \
    pip3 install --upgrade pip setuptools && \
    pip3 install plumbum && \
    rm -r /root/.cache

RUN apk add --no-cache ca-certificates go git musl-dev && \
    go get -u github.com/xenolf/lego && \
    cd /go/src/github.com/xenolf/lego && \
    go build -o /usr/bin/lego . && \
    apk del go git musl-dev && \
    rm -rf /go

ADD run.py /usr/bin/run.py
RUN chmod +x /usr/bin/run.py

CMD ["/usr/bin/run.py"]
