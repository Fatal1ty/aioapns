#!/usr/bin/env python

from setuptools import find_packages, setup

setup(
    name="aioapns",
    version="3.3.1",
    description="An efficient APNs Client Library for Python/asyncio",
    long_description=open("README.rst").read(),
    platforms="all",
    classifiers=[
        "License :: OSI Approved :: Apache Software License",
        "Intended Audience :: Developers",
        "Operating System :: MacOS :: MacOS X",
        "Operating System :: POSIX",
        "Programming Language :: Python :: 3 :: Only",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Development Status :: 5 - Production/Stable",
    ],
    license="Apache License, Version 2.0",
    author="Alexander Tikhonov",
    author_email="random.gauss@gmail.com",
    url="https://github.com/Fatal1ty/aioapns",
    packages=find_packages(exclude=("tests",)),
    package_data={"aioapns": ["py.typed"]},
    install_requires=[
        "h2>=4.0.0",
        "pyOpenSSL>=17.5.0",
        "pyjwt>=2.0.0",
    ],
)
