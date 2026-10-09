"""Downstream chat2api credential; never use an upstream login token here."""
import os

def chat2api_local_key():
    """Console and gateway must inherit the same startup key configuration."""
    return (os.environ.get('CHAT2API_LOCAL_API_KEY') or '').strip() or None
