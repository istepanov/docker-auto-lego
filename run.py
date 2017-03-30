#!/usr/bin/env python3

import os
import asyncio
from datetime import datetime
from asyncio.subprocess import PIPE, STDOUT
from plumbum import RETCODE
from plumbum.cmd import docker, lego, openssl, grep, cut


LEGO_DIR = os.getenv('LEGO_DIR', '/var/lego')
LEGO_DNS = os.getenv('LEGO_DNS', 'route53')
LEGO_DAYS_BEFORE_EXPIRE = int(os.getenv('LEGO_DAYS_BEFORE_EXPIRE', '30'))
LETSENCRYPT_SERVER = os.getenv('LETSENCRYPT_SERVER', '')
DOCKER_GEN_CONTAINER_NAME = os.getenv('DOCKER_GEN_CONTAINER_NAME', '')


def get_containers():
    output = docker[
        'ps',
        '--filter', 'status=running',
        '--filter', 'label=LETSENCRYPT_HOST',
        '--filter', 'label=LETSENCRYPT_EMAIL',
        '--format', '{{.ID}}|{{.Labels}}']()

    containers = []
    for line in output.splitlines():
        (cid, labels) = line.split('|')
        labels = dict(
            (l, v) for l, v in (
                s.split("=") for s in labels.split(","))
            if l in ['LETSENCRYPT_HOST', 'LETSENCRYPT_EMAIL']
        )

        containers.append({
            'id': cid,
            'labels': labels,
        })
    return containers


def check_certificates():
    containers = get_containers()
    print('Found {0} containers that require SSL certs.'.format(len(containers)))

    for container in containers:
        print('Checking certs for container {0}'.format(container['id']))

        letsencrypt_host = container['labels']['LETSENCRYPT_HOST']
        letsencrypt_email = container['labels']['LETSENCRYPT_EMAIL']

        action = None
        public_cert = os.path.join(LEGO_DIR, 'certificates', '{0}.crt'.format(letsencrypt_host))
        private_key = os.path.join(LEGO_DIR, 'certificates', '{0}.key'.format(letsencrypt_host))
        if os.path.isfile(public_cert) or os.path.isfile(private_key):
            expiration_date_string = (openssl['x509', '-in', public_cert, '-text', '-noout'] | grep['Not After'] | cut['-c', '25-'])().strip()
            expiration_date = datetime.strptime(expiration_date_string, '%b %d %H:%M:%S %Y %Z')
            days_to_expire = (expiration_date - datetime.utcnow()).days
            if days_to_expire > LEGO_DAYS_BEFORE_EXPIRE:
                print('The certificate for {0} is up to date, no need for renewal ({1} days left).'.format(letsencrypt_host, days_to_expire))
            else:
                print('The certificate for {0} is about to expire in {1} days. Renewing... '.format(letsencrypt_host, days_to_expire), end='')
                action = 'renew'
        else:
            print('The certificate for {0} is not found. Creating new one... '.format(letsencrypt_host), end='')
            action = 'run'

        if action is not None:
            lego_command = lego[
                '--accept-tos',
                '--path', LEGO_DIR,
                '--email', letsencrypt_email,
                '--domains', letsencrypt_host,
                '--dns', LEGO_DNS,
            ]
            if LETSENCRYPT_SERVER:
                lego_command = lego_command['--server', LETSENCRYPT_SERVER]
            lego_command = lego_command[action]

            return_code = lego_command & RETCODE(FG=True)
            if return_code == 0:
                print('Done.')
                if DOCKER_GEN_CONTAINER_NAME:
                    print('Restarting {0}... '.format(DOCKER_GEN_CONTAINER_NAME), end='')
                    docker['kill', '--signal', 'SIGHUP', DOCKER_GEN_CONTAINER_NAME](retcode = None)
                    print('Done.')
            else:
                print('Failed. Return code: {0}'.format(return_code))


async def cron():
    while True:
        await asyncio.sleep(3600)
        check_certificates()


async def watch_docker_events():
    process = await asyncio.create_subprocess_exec(
        'docker', 'events',
        '-f', 'event=create',
        '-f', 'event=destroy',
        stdout=PIPE, stderr=STDOUT
    )

    while True:
        line = await asyncio.wait_for(process.stdout.readline(), None)
        print(line)
        if line:
            check_certificates()


if __name__ == '__main__':
    check_certificates()

    print('Watching for Docker events...')

    cron_task = asyncio.ensure_future(cron())
    watch_docker_events_task = asyncio.ensure_future(watch_docker_events())

    loop = asyncio.get_event_loop()
    loop.run_until_complete(
        asyncio.wait([cron_task, watch_docker_events_task], return_when=asyncio.FIRST_COMPLETED)
    )
    loop.close()
