#!/usr/bin/env python3

from setuptools import setup, find_packages

setup(
    name='switch_exporter',
    author='Bruce Merry',
    author_email='bmerry@ska.ac.za',
    description='Prometheus exporter for Mellanox switch counters',
    packages=find_packages(),
    setup_requires=['katversion'],
    python_requires='>=3.5',
    install_requires=[
        'aiohttp',
        'async_timeout',
        'asyncssh',
        'attrs',
        'prometheus_client<0.4.0',
        'katsdpservices'
    ],
    entry_points={
        'console_scripts': ['switch-exporter = switch_exporter.server:main']
    },
    use_katversion=True
)
