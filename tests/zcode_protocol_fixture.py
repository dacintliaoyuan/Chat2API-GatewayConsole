"""Synthetic source regions for admission wiring, never executable client code."""
import hashlib

import zcode_hot_sync_protocol as protocol


def sources(shared_name="chunk-FNQWK6LT.js"):
    imports = "".join(
        'import{' + ",".join(exported + " as " + local
                            for local, exported in aliases.items())
        + '}from"' + module + '";'
        for module, aliases in protocol._NATIVE_BINDINGS.items())
    host = (
        imports
        + 'import{Ja as ss,Ka as zo,A as wr,l as lT}from"./' + shared_name + '";'
        + 'function m0(){return 0}i(Gl,"acquireFileLock");'
        + 'var z6=1;i(vr,"atomicWritePrivateTextFile");'
        + 'var qm=1;i(K0,"createProviderRules");'
        + 'var du=1;i(Zk,"isRecord");'
        + 'var fg=class{static{i(this,"NodePersonalProviderConfigRepository")}'
          'read(){return 1}};i(Oi,"snapshotFromUpdate");'
        + 'var lu=class{static{i(this,"NodeProviderConfigRuntime")}start(){return 1}};')
    schema = (
        'function At(e){return e}var tA={},vK={};fake.strict();d();var zv=1;'
        'var V={},cS="$";var l8="ZCODE_FILE_LOCK_TIMEOUT";'
        'export{tA as Ja,vK as Ka,V as A,l8 as l};')
    return host.encode(), schema.encode()


def profile(host, schema):
    return {name: hashlib.sha256(source.encode()).hexdigest()
            for name, source in protocol.protocol_regions(
                host.decode(), schema.decode()).items()}
