#!/usr/bin/env python3

import os
import asyncio
import functools
import signal
import requests
from datetime import datetime
from asyncio.subprocess import PIPE, STDOUT
from plumbum import RETCODE, local
from plumbum.cmd import docker, lego, openssl, grep, cut


LEGO_DIR = os.getenv('LEGO_DIR', '/var/lego')
LEGO_DNS = os.getenv('LEGO_DNS', None)
LEGO_WEBROOT = os.getenv('LEGO_WEBROOT', None)
LEGO_DAYS_BEFORE_EXPIRE = int(os.getenv('LEGO_DAYS_BEFORE_EXPIRE', '30'))
LETSENCRYPT_SERVER = os.getenv('LETSENCRYPT_SERVER', '')
DOCKER_GEN_CONTAINER_NAME = os.getenv('DOCKER_GEN_CONTAINER_NAME', '')


if LEGO_DNS and LEGO_WEBROOT:
    raise ValueError('Cannot specify both LEGO_DNS and LEGO_WEBROOT environment variables simultaneously. Choose one option.')
elif not LEGO_DNS and not LEGO_WEBROOT:
    raise ValueError('Specify either LEGO_DNS or LEGO_WEBROOT environment variable.')


def get_containers():
    output = docker[
        'ps',
        '--filter', 'status=running',
        '--filter', 'label=LETSENCRYPT_DOMAINS',
        '--filter', 'label=LETSENCRYPT_EMAIL',
        '--format', '{{.ID}}|{{.Labels}}']()

    containers = []
    for line in output.splitlines():
        (cid, labels) = line.split('|')
        labels = dict(
            (l, v) for l, v in (
                s.split("=") for s in labels.split(","))
            if l in ['LETSENCRYPT_DOMAINS', 'LETSENCRYPT_EMAIL']
        )

        containers.append({
            'id': cid,
            'labels': labels,
        })
    return containers


def try_get_aws_credentials():
    try:
        print('Trying retrieve AWS credentials... ', end='')
        r = requests.get('http://169.254.169.254/latest/meta-data/iam/security-credentials/', timeout=10)
        r.raise_for_status()
        iam_role = r.text
        if not iam_role:
            return None

        r = requests.get('http://169.254.169.254/latest/meta-data/iam/security-credentials/{0}'.format(iam_role), timeout=10)
        r.raise_for_status()
        response = r.json()
        env = {
            'AWS_ACCESS_KEY_ID': response['AccessKeyId'],
            'AWS_SECRET_ACCESS_KEY': response['SecretAccessKey'],
        }
        print('Success!')
        return env
    except (requests.exceptions.RequestException, ValueError, KeyError):
        print('Failed.')
        return None


def check_certificates():
    containers = get_containers()
    print('Found {0} containers that require SSL certs.'.format(len(containers)))

    for container in containers:
        print('Checking certs for container {0}'.format(container['id']))

        letsencrypt_email = container['labels']['LETSENCRYPT_EMAIL']
        letsencrypt_domains = container['labels']['LETSENCRYPT_DOMAINS']
        letsencrypt_domains = [d.strip() for d in letsencrypt_domains.split(',')]
        letsencrypt_domains = [d for d in letsencrypt_domains if d]

        assert len(letsencrypt_domains) > 0

        action = None
        public_cert = os.path.join(LEGO_DIR, 'certificates', '{0}.crt'.format(letsencrypt_domains[0]))
        private_key = os.path.join(LEGO_DIR, 'certificates', '{0}.key'.format(letsencrypt_domains[0]))
        if os.path.isfile(public_cert) or os.path.isfile(private_key):
            # TODO: instead of calling openssl we can just use '--days' parameter
            expiration_date_string = (openssl['x509', '-in', public_cert, '-text', '-noout'] | grep['Not After'] | cut['-c', '25-'])().strip()
            expiration_date = datetime.strptime(expiration_date_string, '%b %d %H:%M:%S %Y %Z')
            days_to_expire = (expiration_date - datetime.utcnow()).days
            if days_to_expire > LEGO_DAYS_BEFORE_EXPIRE:
                print('The certificate for {0} is up to date, no need for renewal ({1} days left).'.format(letsencrypt_domains[0], days_to_expire))
            else:
                print('The certificate for {0} is about to expire in {1} days. Renewing... '.format(letsencrypt_domains[0], days_to_expire), end='')
                action = 'renew'
        else:
            print('The certificate for {0} is not found. Creating new one... '.format(letsencrypt_domains[0]), end='')
            action = 'run'

        if action is not None:
            env = None

            lego_command = lego[
                '--accept-tos',
                '--path', LEGO_DIR,
                '--email', letsencrypt_email,
            ]
            for domain in letsencrypt_domains:
                lego_command = lego_command['--domains', domain]
            if LEGO_DNS:
                lego_command = lego_command['--dns', LEGO_DNS]
                if LEGO_DNS == 'route53':
                    if 'AWS_ACCESS_KEY_ID' not in os.environ or 'AWS_SECRET_ACCESS_KEY' not in os.environ:
                        env = try_get_aws_credentials()

            elif LEGO_WEBROOT:
                lego_command = lego_command['--webroot', LEGO_WEBROOT]
            if LETSENCRYPT_SERVER:
                lego_command = lego_command['--server', LETSENCRYPT_SERVER]
            lego_command = lego_command[action]

            if env:
                with local.env(**env):
                    return_code = lego_command & RETCODE(FG=True)
            else:
                return_code = lego_command & RETCODE(FG=True)

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


def ask_exit(signame):
    print('Got signal {0}: exit'.format(signame))
    loop.stop()


if __name__ == '__main__':
    check_certificates()

    print('Watching for Docker events...')

    cron_task = asyncio.ensure_future(cron())
    watch_docker_events_task = asyncio.ensure_future(watch_docker_events())

    loop = asyncio.get_event_loop()

    for signame in ('SIGINT', 'SIGTERM'):
        loop.add_signal_handler(getattr(signal, signame), functools.partial(ask_exit, signame))

    loop.run_until_complete(
        asyncio.wait([cron_task, watch_docker_events_task], return_when=asyncio.FIRST_COMPLETED)
    )

    loop.close()
