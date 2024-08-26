#!/usr/bin/env python3

from setuptools import setup

def read_requirements(file):
    with open(file) as f:
        return f.read().splitlines()

setup(
    name="wsdl2file",
    version="0.1",
    author="Tyler MacDonald",
    author_email="tyler@proofserve.com",
    description="Compile a WCF WSDL down to a single file",
    license="MIT License",
    install_requires=read_requirements("requirements.txt"),
    entry_points={
        'console_scripts': [
            'wsdl2file = wsdl2file.command:main'
        ]
    },
    packages = ["wsdl2file"]
)
