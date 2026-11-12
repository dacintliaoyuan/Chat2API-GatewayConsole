"""Synthetic current-layout (3.15.1) sources for admission wiring tests.

The bundle set mirrors the inspected 3.15.1 packaging: the lock and
persistence closure lives in its own chunk, the provider config codec,
repository and runtime wiring live in the host bundle, and the shared
model/selection schemas live in a shared chunk.  Every element is located
by its own runtime naming string, so the fixture can be renamed wholesale
to exercise alias normalization without touching any body.

The persistence atomic write calls the rename-with-retry helper, as the
inspected implementation does, so a duplicate runtime name can be exercised
against a real reference chain (``duplicated``/``unreferenced``/``ambiguous``
add a second ``renameWithRetry``; ``class_duplicated`` does the same for the
repository class, whose only reference is cross-region).
"""
import re

from zcode import hot_sync_protocol as protocol

LOCK_CHUNK = "out/host/chunk-LOCK0000.js"
SHARED_CHUNK = "out/host/chunk-SHARED00.js"
HOST_CHUNK = "out/host/index.js"

_NATIVE_IMPORTS = (
    'import{mkdir as Ko,readFile as jt,readdir as Sn,rmdir as Ge,rm as me,'
    'stat as We,writeFile as qo}from"fs/promises";'
    'import{join as Ue}from"path";'
    'import{setTimeout as Xo}from"timers/promises";'
)


def _lock_chunk(*, atomic_rename=True):
    """The lock/persistence chunk; ``atomic_rename`` keeps the rename call."""
    rename = ",await Si(n,r,t)" if atomic_rename else ""
    return (
        _NATIVE_IMPORTS
        + 'import{Sr as tcode}from"./' + SHARED_CHUNK.rsplit("/", 1)[1] + '";'
        + 'function wn(){let r=new Map;return(e,t,n)=>r}'
          'i(wn,"createLockInstanceObserver");'
        + 'var jo=5*6e4;'
        + 'function Y(r){return typeof r.code=="string"?r.code:void 0}'
          'i(Y,"getErrorCode");'
        + 'function zo(r){return Y(r)==="EEXIST"}i(zo,"isFileExistsError");'
        + 'function zt(r,e){return r}i(zt,"parseLockTimestamp");'
        + 'function Vo(r,e){return{}}i(Vo,"parseLockMetadata");'
        + 'function Jo(r){return!0}i(Jo,"isProcessAlive");'
        + 'async function Xt(r,e,t){return!1}'
          'i(Xt,"isOwnerFileReclaimable");'
        + 'async function Yo(r,e,t){return{removed:!1}}'
          'i(Yo,"removeAbandonedLock");'
        + 'function yn(r,e,t,n){let s=new Error("Timed out");'
          's.code=tcode,s.syscall="mkdir";return s}'
          'i(yn,"createFileLockTimeoutError");'
        + 'async function Vt(r,e,t,n){await Ko(r),await We(r),'
          'await qo(r,"",{flag:"wx"});let s=(await Sn(r)).filter('
          'u=>u.startsWith("owner-")),o=Y(r);return async()=>{'
          'await me(r,{force:!0}),await Ge(r)}}i(Vt,"acquireFileLock");'
        + 'var ki=[25,50,100,200,400],Ei=100,Ti=8e3,'
          'wi=[50,100,200,400,800],Be=new Map;'
        + 'function Dn(r){return typeof r.code=="string"?r.code:void 0}'
          'i(Dn,"getErrorCode");'
        + 'function yi(r){let e=Dn(r);return e==="EPERM"||e==="EBUSY"}'
          'i(yi,"isRetryableRenameError");'
        + 'async function Si(r,e,t){for(let n=0;;n+=1)try{return}catch(s){'
          'throw s}}i(Si,"renameWithRetry");'
        + 'async function er(r,e,t={}){let n=Be.get(r)??Promise.resolve();'
          'let s;try{return await e()}finally{await s?.()}}'
          'i(er,"withFileLock");'
        + 'async function Ri(r,e,t=wi){let n=Ue(r);try{return await er(r,'
          'async()=>{await qo(n,e,"utf-8")' + rename + '},t)}catch(s){'
          'throw s}}i(Ri,"atomicWritePrivateTextFile");'
    )


def _decoy_rename_with_retry():
    """A second ``renameWithRetry`` declaration, unrelated to persistence."""
    return ('async function Zc(r,e,t){for(let n=0;;n+=1)try{await qo(r,e);'
            'return}catch(s){throw s}}i(Zc,"renameWithRetry");')


def _decoy_repository_class():
    """A second repository class carrying the same runtime name."""
    return ('var Zc2=class{static{'
            'i(this,"NodePersonalProviderConfigRepository")}'
            'async read(){return null}};')


def _host_chunk():
    return (
        'import{createHash as JQ}from"crypto";'
        'import{Ub as li,$f as Hr}from"./'
        + SHARED_CHUNK.rsplit("/", 1)[1] + '";'
        + 'var A={object:e=>e,literal:e=>e,array:e=>e,string:e=>e,'
          'unknown:e=>e};'
        + 'var Po={optional:e=>e};'
        + 'var ws={},ux={};'
        + 'var gr=class{constructor(t){this.rules=t}};'
        + 'var up=1,HQ=new Map,jQ=A.object({schemaVersion:A.literal(up),'
          'config:A.object({providerOrder:A.array(A.string().min(1)).optional(),'
          'providerConfigRules:A.unknown(),modelConfigRules:A.unknown(),'
          'defaultModelSelection:Po.optional()}).strict()}).strict();'
        + 'function xF(e){if(!kC(e))return null;return e.schemaVersion}'
          'i(xF,"readSchemaVersion");'
        + 'function VQ(e){let t=xF(e);return t}i(VQ,"requireSchemaVersion");'
        + 'function KQ(e){return e}i(KQ,"normalizeLegacyManualRules");'
        + 'function sC(e){return e}i(sC,"createProviderConfig");'
        + 'function dF(e){return new gr(e)}i(dF,"createProviderRules");'
        + 'function kh(e){return Zs(li.parse(e))}i(kh,"parseModelConfig");'
        + 'function Zs(e){return Hr.fromData(e)}i(Zs,"createModelConfig");'
        + 'function iF(e){return e}'
          'i(iF,"parseZCodeBuiltinProviderConfigRules");'
        + 'function sF(e){return e}i(sF,"parsePersonalProviderConfigMap");'
        + 'function aF(e){return e}'
          'i(aF,"parseZCodeBuiltinModelConfigRules");'
        + 'function cF(e){return e}i(cF,"parsePersonalModelConfigRules");'
        + 'function lQ(e){return e}i(lQ,"createTemplateRules");'
        + 'function rs(e){let t=xF(e);return t}i(rs,"decodeProviderConfigFile");'
        + 'function Ro(e){return{schemaVersion:up,config:e}}'
          'i(Ro,"encodeProviderConfigFile");'
        + 'function kC(e){return typeof e=="object"&&e!==null}'
          'i(kC,"isRecord");'
        + 'function os(e){return Object.freeze({revision:JQ("sha256")'
          '.update(JSON.stringify(Ro(e))).digest("hex")})}'
          'i(os,"snapshotFromUpdate");'
        + 'function XQ(e){return e.code==="ENOENT"}i(XQ,"isFileNotFound");'
        + 'function Th(){return Object.freeze({})}i(Th,"emptyUpdate");'
        + 'async function SC(e){try{return{value:JSON.parse(e)}}catch(t){'
          'if(XQ(t))return null;throw t}}i(SC,"readJsonFileIfExists");'
        + 'var Ah=class{static{i(this,"NodePersonalProviderConfigRepository")}'
          'async read(){return null}async update(t){return t}'
          'onDidChange(t){return()=>{}}dispose(){}};'
        + 'var pp=class{static{i(this,"NodeProviderConfigRuntime")}'
          'constructor(t){this.repository=new Ah(t)}};'
    )


def _shared_chunk():
    return (
        'var e={object:()=>e};'
        'var L=e.object({providerId:e.string().trim().min(1),'
        'modelId:e.string().trim().min(1),options:e.object({'
        'reasoningLevel:e.string().trim().min(1).optional()})'
        '.strict().optional()}).strict(),XS="$";'
        'function Ng(t){if(!t)return"";let n=t.providerId+"/"+t.modelId;'
        'return n}i(Ng,"formatModelPickerValue");'
        'function QS(t){let n=t.trim();return{providerId:n}}'
        'i(QS,"parseModelPickerValue");'
        'var timeoutCode="ZCODE_FILE_LOCK_TIMEOUT";'
        'var Ub=e.object({values:e.array(e.string())}).strict();'
        'var $f=class{static fromData(t){return t}};'
        'export{Ub as Ub,$f as $f,timeoutCode as Sr};'
        'var ws={},ux={};'
        'var x3=e.object({values:e.array(e.string())})'
        '.strict(),jz=e.object({properties:e.array(e.string())})'
        '.strict(),Lz=e.object({properties:e.array(e.string()),'
        'optionSpecs:e.array(e.string())}).strict();'
        'var Zz=e.object({enabled:e.boolean(),properties:ws,'
        'optionSpecs:ux}).strict();'
        'var R3=e.object({...Dt(Zz.shape),properties:jz.nullable()'
        '.optional(),optionSpecs:Lz.nullable().optional()}).strict();'
        'function Dt(t){return t}i(Dt,"spreadShape");'
    )


def _raw_sources(*, atomic_rename=True):
    return {
        LOCK_CHUNK: _lock_chunk(atomic_rename=atomic_rename).encode(),
        HOST_CHUNK: _host_chunk().encode(),
        SHARED_CHUNK: _shared_chunk().encode(),
    }


def sources():
    return _raw_sources()


def duplicated():
    """The same fixture plus an unreferenced second ``renameWithRetry``.

    The duplicate is not referenced by any anchor of the persistence region, so
    the region must still resolve to the declared implementation and every
    region digest must stay identical.
    """
    sources_map = _raw_sources()
    sources_map[LOCK_CHUNK] = (sources_map[LOCK_CHUNK]
                              + _decoy_rename_with_retry().encode())
    return sources_map


def unreferenced():
    """A duplicate with no reference chain from any region anchor.

    The atomic write no longer calls the rename helper, so neither candidate can
    be proven part of the persistence region and admission must fail closed.
    """
    sources_map = _raw_sources(atomic_rename=False)
    sources_map[LOCK_CHUNK] = (sources_map[LOCK_CHUNK]
                              + _decoy_rename_with_retry().encode())
    return sources_map


def ambiguous():
    """A duplicate referenced by a second region anchor as well.

    Both ``renameWithRetry`` declarations are reachable from the region, so no
    candidate can be proven and admission must fail closed.
    """
    sources_map = duplicated()
    sources_map[LOCK_CHUNK] = sources_map[LOCK_CHUNK].replace(
        b'function yi(r){let e=Dn(r);return e==="EPERM"||e==="EBUSY"}',
        b'function yi(r){let e=Dn(r);return Zc(r),e==="EPERM"||e==="EBUSY"}')
    return sources_map


def class_duplicated():
    """The same fixture plus an unreferenced second repository class.

    The runtime wiring constructs the repository class, but that reference lives
    in another region, so no anchor of the repository region itself can prove
    either candidate: admission must fail closed.  A cross-region reference is
    deliberately not accepted as a disambiguation basis.
    """
    sources_map = _raw_sources()
    sources_map[HOST_CHUNK] = (sources_map[HOST_CHUNK]
                              + _decoy_repository_class().encode())
    return sources_map


def renamed():
    """The same fixture with every minified binding and chunk renamed."""
    bindings = {
        "wn": "Qa", "Y": "Zb", "zo": "Lc", "zt": "Md", "Vo": "Ne",
        "Jo": "Of", "Xt": "Pg", "Yo": "Qh", "yn": "Ri", "Vt": "Sj",
        "jo": "Tk", "ki": "Ul", "Ei": "Vm", "Ti": "Wn", "wi": "Xo",
        "Be": "Yp", "Dn": "Zq", "yi": "Ar", "Si": "Bs", "er": "Ct",
        "Ri": "Du", "Ko": "Ev", "jt": "Fw", "Sn": "Gx", "Ge": "Hy",
        "me": "Iz", "We": "Ja", "qo": "Kb", "Ue": "Ld", "Xo": "Me",
        "tcode": "Nf", "Sr": "Og", "xF": "Ph", "VQ": "Qi", "KQ": "Rj",
        "sC": "Sk", "dF": "Tl", "kh": "Um", "Zs": "Vn", "iF": "Wo",
        "sF": "Xp", "aF": "Yq", "cF": "Zr", "lQ": "As", "rs": "Bt",
        "Ro": "Cu", "kC": "Dv", "os": "Ew", "XQ": "Fx", "Th": "Gy",
        "SC": "Hz", "Ah": "Ia2", "pp": "Jb2", "Ng": "Kc2", "QS": "Ld2",
        "L": "Me2", "XS": "Nf2", "up": "Og2", "HQ": "Ph2", "jQ": "Qi2",
        "timeoutCode": "Rj2", "A": "Sk2", "Po": "Tl2", "ws": "Um2",
        "ux": "Vn2", "Ub": "Za2", "$f": "Zb2", "gr": "Wo2", "li": "Xp2", "Hr": "Yq2", "JQ": "Zr2",
        "e": "As2", "Dt": "Bt2", "x3": "Cu2", "jz": "Dv2", "Lz": "Ew2",
        "Zz": "Fx2", "R3": "Gy2",
    }
    chunks = {"chunk-LOCK0000.js": "chunk-RELOCK.js",
              "chunk-SHARED00.js": "chunk-RESHAR.js"}
    sources_map = {}
    for name, raw in _raw_sources().items():
        text = raw.decode("utf-8")
        text = re.sub(r"[\w$]+",
                      lambda match: bindings.get(match.group(0),
                                                match.group(0)), text)
        for old, new in chunks.items():
            text = text.replace(old, new)
        for old, new in chunks.items():
            name = name.replace(old, new)
        sources_map[name] = text.encode()
    return sources_map


def profile(bundles):
    """Derived digests for the fixture's own text (never trusted blindly).

    The bundle map is keyed by entry name (as ``_bundles`` reads it), so the
    fixture's asar paths are stripped first.
    """
    cache = {}
    decoded = {name.rsplit("/", 1)[-1]: text.decode("utf-8")
               for name, text in bundles.items()}
    profile_map = {}
    for name, region in protocol._CURRENT_REGIONS.items():
        file, missing = protocol._region_file(decoded, region)
        if file is None:
            raise AssertionError("fixture region unresolved: %s (%s)"
                                 % (name, missing))
        elements = protocol._region_elements(decoded, region, file)[0]
        collected = protocol._closure(decoded, cache, file, elements,
                                    region["depth"])
        normalized = [(label, protocol._normalize(decoded, cache, file, body))
                      for label, body in collected]
        profile_map[name] = protocol._digest(normalized)
    return profile_map
