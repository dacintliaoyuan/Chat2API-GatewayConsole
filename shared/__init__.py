# -*- coding: utf-8 -*-
"""Side-effect-free protocol, parsing and pure-rule helpers shared by both apps.

The package is bundled with the local and the cloud gateway. It owns no host,
state, file, network, credential, broker or operation-manager code, and importing
it must not import any of its submodules.
"""
