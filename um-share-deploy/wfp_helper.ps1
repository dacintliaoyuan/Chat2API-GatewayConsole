# WFP helper v3b (app-id TCP scoping). Applies to the dedicated share image
# (um-share-python.exe):
#   ALE_AUTH_CONNECT_V4 : permit 127.0.0.1:<port> (TCP) + block all else
#   ALE_AUTH_CONNECT_V6 : block all (TCP)
# TCP is fully covered (loopback AND remote, IPv4 + IPv6). UDP cannot be filtered
# by image at the transport layer (OUTBOUND_TRANSPORT has no ALE_APP_ID field ->
# FWP_E_CONDITION_NOT_FOUND), so remote UDP/IPv6 is blocked instead by a
# Windows-Firewall PROGRAM outbound rule (see deploy script); loopback UDP is a
# documented residual (Windows Firewall does not filter loopback, and no local
# gateway service listens on UDP).
# Filters are non-persistent (reboot clears them) with fixed GUIDs.
# -Port is REQUIRED for `add`/`strong` (no silent default); `remove`/`verify`
# address the same filters by GUID and take it only for display.
param(
    [Parameter(Mandatory = $true)][ValidateSet('add', 'remove', 'verify', 'strong')][string]$Action,
    [Parameter(Mandatory = $true)][string]$AppExePath,
    # Dedicated-backend permit port (Revision 6 R6-A1). There is NO default:
    # `add` and `strong` REFUSE without an explicit -Port, because a silent
    # default would install/verify a permit for a port nobody chose - the
    # historical 8000 is the PRIVATE chat2api instance. `remove`/`verify` work
    # by the fixed filter GUIDs and do not need a port (0 = not given).
    [int]$Port = 0
)

$requiresPort = ($Action -eq 'add' -or $Action -eq 'strong')
if ($requiresPort -and ($Port -lt 1 -or $Port -gt 65535)) {
    throw ("-Port is required for -Action {0}: pass the dedicated share-backend permit port explicitly (1-65535); there is no default. got: {1}" -f $Action, $Port)
}
if (-not $requiresPort -and $Port -ne 0 -and ($Port -lt 1 -or $Port -gt 65535)) {
    throw ("invalid -Port {0}: must be 1-65535 (or omit it for -Action {1})" -f $Port, $Action)
}
if ($requiresPort) {
    # The permit scope is app-id + 127.0.0.1 + exactly this port; it never
    # widens and never falls back to another backend's port.
    Write-Output ("[WFP] permit port: {0}" -f $Port)
}

$ErrorActionPreference = 'Stop'

Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
using System.Text;

public static class WfpApp3 {
    [DllImport("fwpuclnt.dll", CharSet = CharSet.Unicode)]
    public static extern uint FwpmEngineOpen0(IntPtr serverName, uint authnService,
        IntPtr authIdentity, IntPtr session, out IntPtr engineHandle);
    [DllImport("fwpuclnt.dll", CharSet = CharSet.Unicode)]
    public static extern uint FwpmFilterAdd0(IntPtr engineHandle, ref FWPM_FILTER0 filter,
        IntPtr sd, out ulong id);
    [DllImport("fwpuclnt.dll", CharSet = CharSet.Unicode)]
    public static extern uint FwpmFilterDeleteByKey0(IntPtr engineHandle, ref Guid filterKey);
    [DllImport("fwpuclnt.dll", CharSet = CharSet.Unicode)]
    public static extern uint FwpmFilterGetByKey0(IntPtr engineHandle, ref Guid filterKey,
        ref IntPtr filter);
    [DllImport("fwpuclnt.dll", CharSet = CharSet.Unicode)]
    public static extern uint FwpmEngineClose0(IntPtr engineHandle);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
    public static extern uint QueryDosDevice(string deviceName, StringBuilder target, int max);
    [DllImport("fwpuclnt.dll", CharSet = CharSet.Unicode)]
    public static extern uint FwpmGetAppIdFromFileName0(string fileName, out IntPtr appIdBlob);
    [DllImport("fwpuclnt.dll")]
    public static extern uint FwpmFreeMemory0(ref IntPtr p);

    [StructLayout(LayoutKind.Sequential)]
    public struct FWP_BYTE_BLOB { public uint size; public IntPtr data; }
    [StructLayout(LayoutKind.Sequential)]
    public struct FWP_VALUE0 { public uint type; public IntPtr unionField; }
    [StructLayout(LayoutKind.Sequential)]
    public struct FWPM_DISPLAY_DATA0 { public IntPtr name; public IntPtr description; }
    [StructLayout(LayoutKind.Sequential)]
    public struct FWP_CONDITION_VALUE0 { public uint type; public IntPtr unionField; }
    [StructLayout(LayoutKind.Sequential)]
    public struct FWPM_FILTER_CONDITION0 {
        public Guid fieldKey; public uint matchType; public FWP_CONDITION_VALUE0 conditionValue;
    }
    [StructLayout(LayoutKind.Explicit, Size = 24)]
    public struct FWPM_ACTION0 {
        [FieldOffset(0)] public uint type;
        [FieldOffset(8)] public Guid calloutKey;
        [FieldOffset(8)] public ulong rawContext;
    }
    [StructLayout(LayoutKind.Sequential)]
    public struct FWPM_FILTER0 {
        public Guid filterKey;
        public FWPM_DISPLAY_DATA0 displayData;
        public uint flags;
        public IntPtr providerKey;
        public FWP_BYTE_BLOB providerData;
        public Guid layerKey;
        public Guid subLayerKey;
        public FWP_VALUE0 weight;
        public uint numFilterConditions;
        public IntPtr filterCondition;
        public FWPM_ACTION0 action;
        public IntPtr rawContext;
        public IntPtr reserved;
        public ulong filterId;
        public FWP_VALUE0 effectiveWeight;
    }

    public const uint MATCH_EQUAL = 0;
    public const uint ACT_BLOCK = 0x1001;
    public const uint ACT_PERMIT = 0x1002;
    public const uint T_UINT16 = 2;
    public const uint T_UINT32 = 3;
    public const uint T_UINT64 = 4;
    public const uint T_BYTE_BLOB = 12;

    public static Guid LayerV4 = new Guid("c38d57d1-05a7-4c33-904f-7fbceee60e82");
    public static Guid LayerV6 = new Guid("4a72393b-319f-44bc-84c3-ba54dcb3b6b4");
    public static Guid SubUniversal = new Guid("eebecc03-ced4-4380-819a-2734397b2b74");
    public static Guid FAppId = new Guid("d78e1e87-8644-4ea5-9437-d809ecefc971");
    public static Guid FAddr = new Guid("b235ae9a-1d64-49b8-a44c-5ff3d9095045");
    public static Guid FPort = new Guid("c35a604d-d22b-4e1a-91b4-68f674ee674b");

    public static string NtPathOf(string windowsPath) {
        StringBuilder sb = new StringBuilder(1024);
        string drive = windowsPath.Substring(0, 2);
        QueryDosDevice(drive, sb, sb.Capacity);
        return sb.ToString() + windowsPath.Substring(2);
    }

    private static IntPtr MakeAppIdBlob(string exePath) {
        IntPtr blobPtr = IntPtr.Zero;
        uint rc = FwpmGetAppIdFromFileName0(exePath, out blobPtr);
        if (rc != 0) { throw new Exception("FwpmGetAppIdFromFileName0 0x" + rc.ToString("X")); }
        return blobPtr; // FWP_BYTE_BLOB* owned by BFE, freed via FwpmFreeMemory0
    }

    private static IntPtr MakeValue(uint type, ulong inlineValue, IntPtr ptr) {
        FWP_VALUE0 v;
        v.type = type;
        v.unionField = ptr != IntPtr.Zero ? ptr : (IntPtr)inlineValue;
        IntPtr p = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(FWP_VALUE0)));
        Marshal.StructureToPtr(v, p, false);
        return p;
    }

    public static uint[] AddAll(IntPtr engine, string exePath, Guid permitKey, Guid blockV4Key,
        Guid blockV6Key, uint port) {
        uint[] rcs = new uint[3];
        IntPtr appId = MakeAppIdBlob(exePath);

        // V4 permit: app-id + 127.0.0.1 + <port>
        FWPM_FILTER0 f = new FWPM_FILTER0();
        f.filterKey = permitKey;
        f.displayData.name = Marshal.StringToHGlobalUni("umshare-public-permit-um-" + port.ToString());
        f.displayData.description = Marshal.StringToHGlobalUni("UM share isolation (app-scoped)");
        f.layerKey = LayerV4;
        f.subLayerKey = SubUniversal;
        f.weight.type = T_UINT64;
        IntPtr w = Marshal.AllocHGlobal(8);
        Marshal.WriteInt64(w, unchecked((long)0x0000F00000000000UL));
        f.weight.unionField = w;
        f.numFilterConditions = 3;
        f.filterCondition = Marshal.AllocHGlobal(3 * Marshal.SizeOf(typeof(FWPM_FILTER_CONDITION0)));
        FWPM_FILTER_CONDITION0 c0 = new FWPM_FILTER_CONDITION0();
        c0.fieldKey = FAppId; c0.matchType = MATCH_EQUAL;
        c0.conditionValue.type = T_BYTE_BLOB; c0.conditionValue.unionField = appId;
        Marshal.StructureToPtr(c0, f.filterCondition, false);
        FWPM_FILTER_CONDITION0 c1 = new FWPM_FILTER_CONDITION0();
        c1.fieldKey = FAddr; c1.matchType = MATCH_EQUAL;
        c1.conditionValue.type = T_UINT32; c1.conditionValue.unionField = (IntPtr)0x7f000001;
        Marshal.StructureToPtr(c1, (IntPtr)((long)f.filterCondition + Marshal.SizeOf(typeof(FWPM_FILTER_CONDITION0))), false);
        FWPM_FILTER_CONDITION0 c2 = new FWPM_FILTER_CONDITION0();
        c2.fieldKey = FPort; c2.matchType = MATCH_EQUAL;
        c2.conditionValue.type = T_UINT16; c2.conditionValue.unionField = (IntPtr)port;
        Marshal.StructureToPtr(c2, (IntPtr)((long)f.filterCondition + 2 * Marshal.SizeOf(typeof(FWPM_FILTER_CONDITION0))), false);
        f.action.type = ACT_PERMIT;
        ulong id;
        rcs[0] = FwpmFilterAdd0(engine, ref f, IntPtr.Zero, out id);
        Marshal.FreeHGlobal(f.displayData.name);
        Marshal.FreeHGlobal(f.displayData.description);
        Marshal.FreeHGlobal(f.filterCondition);
        Marshal.FreeHGlobal(w);

        // V4 block-all for the same app id
        f = new FWPM_FILTER0();
        f.filterKey = blockV4Key;
        f.displayData.name = Marshal.StringToHGlobalUni("umshare-public-block-all-v4");
        f.displayData.description = Marshal.StringToHGlobalUni("UM share isolation (app-scoped)");
        f.layerKey = LayerV4;
        f.subLayerKey = SubUniversal;
        f.weight.type = T_UINT64;
        w = Marshal.AllocHGlobal(8);
        Marshal.WriteInt64(w, unchecked((long)0x0000E00000000000UL));
        f.weight.unionField = w;
        f.numFilterConditions = 1;
        f.filterCondition = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(FWPM_FILTER_CONDITION0)));
        c0 = new FWPM_FILTER_CONDITION0();
        c0.fieldKey = FAppId; c0.matchType = MATCH_EQUAL;
        c0.conditionValue.type = T_BYTE_BLOB; c0.conditionValue.unionField = appId;
        Marshal.StructureToPtr(c0, f.filterCondition, false);
        f.action.type = ACT_BLOCK;
        rcs[1] = FwpmFilterAdd0(engine, ref f, IntPtr.Zero, out id);
        Marshal.FreeHGlobal(f.displayData.name);
        Marshal.FreeHGlobal(f.displayData.description);
        Marshal.FreeHGlobal(f.filterCondition);
        Marshal.FreeHGlobal(w);

        // V6 block-all for the same app id
        f = new FWPM_FILTER0();
        f.filterKey = blockV6Key;
        f.displayData.name = Marshal.StringToHGlobalUni("umshare-public-block-all-v6");
        f.displayData.description = Marshal.StringToHGlobalUni("UM share isolation (app-scoped)");
        f.layerKey = LayerV6;
        f.subLayerKey = SubUniversal;
        f.weight.type = T_UINT64;
        w = Marshal.AllocHGlobal(8);
        Marshal.WriteInt64(w, unchecked((long)0x0000E00000000000UL));
        f.weight.unionField = w;
        f.numFilterConditions = 1;
        f.filterCondition = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(FWPM_FILTER_CONDITION0)));
        c0 = new FWPM_FILTER_CONDITION0();
        c0.fieldKey = FAppId; c0.matchType = MATCH_EQUAL;
        c0.conditionValue.type = T_BYTE_BLOB; c0.conditionValue.unionField = appId;
        Marshal.StructureToPtr(c0, f.filterCondition, false);
        f.action.type = ACT_BLOCK;
        rcs[2] = FwpmFilterAdd0(engine, ref f, IntPtr.Zero, out id);
        Marshal.FreeHGlobal(f.displayData.name);
        Marshal.FreeHGlobal(f.displayData.description);
        Marshal.FreeHGlobal(f.filterCondition);
        Marshal.FreeHGlobal(w);

        IntPtr appIdTmp = appId;
        FwpmFreeMemory0(ref appIdTmp);
        // Idempotent re-add: treat FWP_E_ALREADY_EXISTS as success.
        for (int i = 0; i < 3; i++) {
            if (rcs[i] == 0x80320009u) { rcs[i] = 0; }
        }
        return rcs;
    }

    public static uint[] RemoveAll(IntPtr engine, Guid permitKey, Guid blockV4Key, Guid blockV6Key) {
        uint[] rcs = new uint[3];
        rcs[0] = FwpmFilterDeleteByKey0(engine, ref permitKey);
        rcs[1] = FwpmFilterDeleteByKey0(engine, ref blockV4Key);
        rcs[2] = FwpmFilterDeleteByKey0(engine, ref blockV6Key);
        return rcs;
    }

    // A filter query failure is NOT "the filter is missing". A non-elevated
    // caller gets ERROR_ACCESS_DENIED (0x5) and would otherwise be printed as
    // MISSING (a false negative: "cannot read" folded into "not in place").
    // FWP_E_FILTER_NOT_FOUND (0x80320003) is the ONLY code that means the
    // filter is really absent; every other code is an unreadable/error result
    // and carries its own rc so the reason stays visible.
    public static string FilterQueryFailure(uint rc) {
        if (rc == 0x80320003) { return "MISSING(FWP_E_FILTER_NOT_FOUND 0x80320003)"; }
        if (rc == 5) { return "UNREADABLE(ACCESS_DENIED 0x5)"; }
        return "ERROR(0x" + rc.ToString("X") + ")";
    }

    public static string FilterState(IntPtr engine, ref Guid key) {
        IntPtr ptr = IntPtr.Zero;
        uint rc = FwpmFilterGetByKey0(engine, ref key, ref ptr);
        if (ptr != IntPtr.Zero) { IntPtr tmp = ptr; FwpmFreeMemory0(ref tmp); }
        if (rc == 0) { return "present"; }
        return FilterQueryFailure(rc);
    }

    public static string[] VerifyAll(IntPtr engine, Guid permitKey, Guid blockV4Key, Guid blockV6Key) {
        string[] states = new string[3];
        states[0] = FilterState(engine, ref permitKey);
        states[1] = FilterState(engine, ref blockV4Key);
        states[2] = FilterState(engine, ref blockV6Key);
        return states;
    }

    // Strong verification: presence + expected action/layer + app-id blob equal
    // to the CURRENT image path, PLUS the permit filter must carry exactly the
    // 127.0.0.1:<port> destination (addr+port) conditions and each block-all
    // must contain ONLY the app-id condition (any narrowing = tamper/mis-scope).
    // Mapping mode: 0=permit(v4:<port>), 1=block-v4-all, 2=block-v6-all.
    public static string StrongCheck(IntPtr engine, string exePath, Guid permitKey,
        Guid blockV4Key, Guid blockV6Key, uint port) {
        System.Text.StringBuilder sb = new System.Text.StringBuilder();
        IntPtr currentAppId = IntPtr.Zero;
        uint rc = FwpmGetAppIdFromFileName0(exePath, out currentAppId);
        if (rc != 0) { return ("APPID-ERROR 0x" + rc.ToString("X")); }
        try {
            Guid[] keys = { permitKey, blockV4Key, blockV6Key };
            uint[] expectedAction = { ACT_PERMIT, ACT_BLOCK, ACT_BLOCK };
            Guid[] expectedLayers = { LayerV4, LayerV4, LayerV6 };
            bool allOk = true;
            string firstFailure = null;
            for (int i = 0; i < keys.Length; i++) {
                IntPtr filterPtr = IntPtr.Zero;
                uint grc = FwpmFilterGetByKey0(engine, ref keys[i], ref filterPtr);
                if (grc != 0) {
                    string tag = "filter" + i + "=" + FilterQueryFailure(grc);
                    sb.Append(tag + ";");
                    if (firstFailure == null) { firstFailure = tag; }
                    allOk = false; continue;
                }
                bool ok = true;
                string act = "?", layer = "?", appid = "NO-APPID-COND", scope = "";
                try {
                    FWPM_FILTER0 f = (FWPM_FILTER0)Marshal.PtrToStructure(filterPtr, typeof(FWPM_FILTER0));
                    act = (f.action.type == expectedAction[i]) ? "OK" : ("BAD-ACTION(0x" + f.action.type.ToString("X") + ")");
                    if (f.action.type != expectedAction[i]) { ok = false; }
                    layer = (f.layerKey == expectedLayers[i]) ? "OK" : "BAD-LAYER";
                    if (f.layerKey != expectedLayers[i]) { ok = false; }
                    bool appidOk = false;
                    bool addrOk = (i != 0);   // permit must ALSO carry addr+port
                    bool portOk = (i != 0);
                    uint condCount = f.numFilterConditions;
                    for (uint c = 0; c < condCount; c++) {
                        FWPM_FILTER_CONDITION0 cond = (FWPM_FILTER_CONDITION0)Marshal.PtrToStructure(
                            (IntPtr)((long)f.filterCondition + c * Marshal.SizeOf(typeof(FWPM_FILTER_CONDITION0))),
                            typeof(FWPM_FILTER_CONDITION0));
                        if (cond.fieldKey == FAppId && cond.conditionValue.type == T_BYTE_BLOB) {
                            FWP_BYTE_BLOB blob = (FWP_BYTE_BLOB)Marshal.PtrToStructure(cond.conditionValue.unionField, typeof(FWP_BYTE_BLOB));
                            FWP_BYTE_BLOB cur = (FWP_BYTE_BLOB)Marshal.PtrToStructure(currentAppId, typeof(FWP_BYTE_BLOB));
                            appidOk = (cur.size == blob.size);
                            if (appidOk) {
                                byte[] a = new byte[(int)blob.size]; byte[] b = new byte[(int)cur.size];
                                Marshal.Copy(blob.data, a, 0, (int)blob.size);
                                Marshal.Copy(cur.data, b, 0, (int)cur.size);
                                for (int x = 0; x < a.Length; x++) { if (a[x] != b[x]) { appidOk = false; break; } }
                            }
                            appid = appidOk ? "MATCH" : "MISMATCH";
                        } else if (i == 0 && cond.fieldKey == FAddr && cond.conditionValue.type == T_UINT32) {
                            addrOk = ((ulong)cond.conditionValue.unionField.ToInt32() & 0xFFFFFFFFL) == 0x7F000001UL;
                        } else if (i == 0 && cond.fieldKey == FPort && cond.conditionValue.type == T_UINT16) {
                            portOk = (cond.conditionValue.unionField.ToInt64() == port);
                        }
                    }
                    if (!appidOk) { ok = false; }
                    if (i == 0) {
                        // permit must be exactly appid+addr(127.0.0.1)+port(<port>)
                        if (!addrOk || !portOk || (int)condCount != 3) {
                            scope = " addr=" + (addrOk ? "OK" : "BAD") + " port=" + (portOk ? "OK" : "BAD") + " conds=" + condCount;
                            ok = false;
                        }
                    } else {
                        // block-all must be ONLY the app-id condition
                        if ((int)condCount != 1) {
                            scope = " conds=" + (int)condCount + "(expect 1: appid only)";
                            ok = false;
                        }
                    }
                    sb.Append("filter" + i + "=[action:" + act + " layer:" + layer + " appid:" + appid + scope + "];");
                } finally {
                    IntPtr t = filterPtr; FwpmFreeMemory0(ref t);
                }
                if (!ok) {
                    if (firstFailure == null) { firstFailure = "filter" + i; }
                    allOk = false;
                }
            }
            if (allOk) { sb.Append(" STRONG=OK"); }
            else { sb.Append(" STRONG=FAIL reason=" + (firstFailure == null ? "unknown" : firstFailure)); }
            return sb.ToString();
        } finally {
            IntPtr tmp = currentAppId; FwpmFreeMemory0(ref tmp);
        }
    }
}
"@

$permitKey = [Guid]'70fa56bb-72bb-4a91-80b1-306293c827eb'
$blockV4Key = [Guid]'9aa299fc-d635-4514-a46e-c7bb049563c2'
$blockV6Key = [Guid]'d13a61e2-e2b2-4bba-8ed5-45a8a2e4653a'
# remove/verify address the filters by their FIXED GUIDs, so the port is
# display-only there (no port suffix in the name when it was not given).
$names = $(if ($Port -ge 1) { "permit-tcp-$Port" } else { 'permit-tcp' }), 'block-tcp-v4', 'block-tcp-v6'

$engine = [IntPtr]::Zero
$rc = [WfpApp3]::FwpmEngineOpen0([IntPtr]::Zero, 10, [IntPtr]::Zero, [IntPtr]::Zero, [ref]$engine)
if ($rc -ne 0) { throw ("FwpmEngineOpen0 failed: 0x{0:X}" -f $rc) }
try {
    if ($Action -eq 'verify') {
        # states are rc-aware strings: present / MISSING(FWP_E_FILTER_NOT_FOUND
        # 0x80320003) / UNREADABLE(ACCESS_DENIED 0x5) / ERROR(0x...). A read
        # failure (e.g. non-elevated 0x5) is NEVER folded into MISSING.
        $states = [WfpApp3]::VerifyAll($engine, $permitKey, $blockV4Key, $blockV6Key)
        for ($i = 0; $i -lt $states.Length; $i++) {
            Write-Output ("[WFP] " + $names[$i] + ": " + $states[$i])
        }
    } elseif ($Action -eq 'strong') {
        # strong: presence + action/layer + app-id condition equal to the current
        # image path (catches the stale-path / tampered-filter case)
        $detail = [WfpApp3]::StrongCheck($engine, $AppExePath, $permitKey, $blockV4Key, $blockV6Key, [uint32]$Port)
        Write-Output ("[WFP-STRONG] " + $detail)
        if ($detail -notlike '*STRONG=OK*') {
            $failReason = 'unknown'
            if ($detail -match 'STRONG=FAIL reason=([^;]+)') { $failReason = $Matches[1] }
            Write-Output ("[WFP-STRONG] RESULT=FAIL reason=" + $failReason)
            exit 1
        }
        Write-Output '[WFP-STRONG] RESULT=OK'
    } elseif ($Action -eq 'add') {
        $rcs = [WfpApp3]::AddAll($engine, $AppExePath, $permitKey, $blockV4Key, $blockV6Key, [uint32]$Port)
        for ($i = 0; $i -lt 3; $i++) {
            if ($rcs[$i] -ne 0 -and $rcs[$i] -ne 0x80320009) {
                throw ("filter {0} failed: 0x{1:X}" -f $names[$i], $rcs[$i])
            }
        }
        # Re-verify immediately: install is only "ok" when every filter is queryable.
        $present = [WfpApp3]::VerifyAll($engine, $permitKey, $blockV4Key, $blockV6Key)
        for ($i = 0; $i -lt $present.Length; $i++) {
            if ($present[$i] -ne 'present') { throw ("filter {0} not queryable after add: {1}" -f $names[$i], $present[$i]) }
        }
        Write-Output ("[WFP] OK: TCP permit(v4:{0})+block(v4/v6) installed and verified" -f $Port)
    } else {
        $rcs = [WfpApp3]::RemoveAll($engine, $permitKey, $blockV4Key, $blockV6Key)
        for ($i = 0; $i -lt 3; $i++) {
            if ($rcs[$i] -eq 0) { Write-Output ("[WFP] removed " + $names[$i]) }
            elseif ($rcs[$i] -eq 0x80320003) { Write-Output ("[WFP] " + $names[$i] + " not present") }
            else { Write-Output ("[WFP] {0} delete rc=0x{1:X}" -f $names[$i], $rcs[$i]) }
        }
    }
} finally {
    if ($engine -ne [IntPtr]::Zero) { [void][WfpApp3]::FwpmEngineClose0($engine) }
}
exit 0
