# -*- coding: utf-8 -*-
"""Cloud application package (E19-1-11).

This package is the cloud composition root: the UM share host, Key API,
broker client, dedicated 8110 backend and private management listener. It is
independent of the local (Windows) console package and never imports its hosts,
adapters, state readers or model-sync modules.
"""
