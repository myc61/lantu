# -*- coding: utf-8 -*-
from distutils.core import setup
from catkin_pkg.python_setup import generate_distutils_setup

d = generate_distutils_setup(
    packages=['chassis_following', 'chassis_following.states'],
    package_dir={'': 'scripts'},
)

setup(**d)
