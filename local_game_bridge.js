// Runtime adapter for the hash-verified CK3 1.20.1.2 executable.
// All changes are held by this Frida session and revert when it detaches.
const base = Process.mainModule.base;
const config = __LOCAL_CONFIG__;
const localName = Memory.allocUtf8String(config.username);
const localId = Memory.allocUtf8String('ck3-local-host');
const localCredential = Memory.allocUtf8String('local-session');
const assignString = new NativeFunction(base.add(0x858470), 'pointer', ['pointer', 'pointer', 'uint64']);
const beginLogin = new NativeFunction(base.add(0x3d63a40), 'void', ['pointer']);
let localSettings = false;
let loginRequested = false;
let lastStatus = '';
let lastProfileMatch = '';
let nextProfileCheck = 0;
let rtConnected = null;
const installed = new Set();

function wrapper() {
    const value = base.add(0x5cd1e78).readPointer();
    return value.isNull() ? null : value;
}

function status() {
    const value = wrapper();
    if (value === null) return { initialized: false, local_settings: localSettings };
    const impl = value.readPointer();
    if (impl.isNull()) return { initialized: false, local_settings: localSettings };
    const rt = impl.add(0x20).readPointer();
    let connected = false;
    if (!rt.isNull() && rtConnected !== null) {
        const handle = rt.add(0x10).readPointer();
        connected = !handle.isNull() && !!rtConnected(handle);
    }
    return { initialized: true, local_settings: localSettings, login_requested: loginRequested,
        auth_state: impl.add(0x2e0).readU32(), connected: connected,
        wrapper: value.toString(), impl: impl.toString() };
}

function cppString(address, decodeText = true) {
    const length = address.add(0x10).readU64().toNumber();
    const capacity = address.add(0x18).readU64().toNumber();
    if (length > 65535 || length > capacity) throw new Error('Invalid bounded C++ string');
    const data = capacity < 16 ? address : address.readPointer();
    return { text: decodeText ? data.readUtf8String(length) : null, hex: Array.from(new Uint8Array(data.readByteArray(length)))
        .map(value => value.toString(16).padStart(2, '0')).join(''), length: length };
}

function sessionProfile() {
    const value = wrapper();
    if (!localSettings || value === null) return null;
    const impl = value.readPointer();
    const match = cppString(impl.add(0x1d8)).text;
    if (!match || match !== cppString(impl.add(0x1b8)).text) return null;
    const session = base.add(0x5cd6250);
    const compatibility = [0x1e8, 0x208, 0x228].map(offset => cppString(session.add(offset), false));
    if (compatibility[0].length !== 16 || compatibility[1].length !== 16 || compatibility[2].length > 33) {
        return null;
    }
    return {
        exe_sha256: config.exe_sha256, host: config.host, port: config.port, ssl: false,
        match_id: match, host_user_id: cppString(impl.add(0x1f8)).text,
        handshake: {
            compatibility_a_hex: compatibility[0].hex,
            compatibility_b_hex: compatibility[1].hex,
            compatibility_c_hex: compatibility[2].hex.padEnd(66, '0'),
            handshake_flag: 1, platform: 0x4e4d5453,
        },
    };
}

function onTick() {
    if (!localSettings) return;
    try {
        const current = status();
        const summary = JSON.stringify({ auth_state: current.auth_state, connected: current.connected });
        if (summary !== lastStatus) {
            lastStatus = summary;
            send({ event: 'local_game_status', ...current });
        }
        if (current.connected && Date.now() >= nextProfileCheck) {
            nextProfileCheck = Date.now() + 1000;
            const profile = sessionProfile();
            if (profile !== null && profile.match_id !== lastProfileMatch) {
                lastProfileMatch = profile.match_id;
                send({ event: 'local_session_profile', profile: profile });
            }
        }
        if (loginRequested || !current.initialized) return;
        loginRequested = true;
        const value = wrapper();
        const impl = value.readPointer();
        assignString(impl.add(0x2e8), localId, 14);
        assignString(impl.add(0x308), localCredential, 13);
        beginLogin(value);
        send({ event: 'local_game_login_requested' });
    } catch (error) {
        send({ event: 'local_bridge_error', message: String(error), stack: error.stack });
    }
}

function installNakama(module) {
    if (installed.has(module.name)) return;
    installed.add(module.name);
    rtConnected = new NativeFunction(module.getExportByName('NRtClient_isConnected'), 'bool', ['pointer']);
    Interceptor.attach(module.getExportByName('createDefaultNakamaClient'), {
        onEnter(args) {
            const parameters = args[0];
            const host = parameters.add(8).readPointer().readUtf8String();
            const port = parameters.add(16).readU32();
            const ssl = parameters.add(20).readU8() !== 0;
            localSettings = host === config.host && port === config.port && !ssl;
            send({ event: 'game_backend_settings', host: host, port: port, ssl: ssl,
                local_settings: localSettings });
        }
    });
    Interceptor.attach(module.getExportByName('NClient_tick'), { onEnter: onTick });
    Interceptor.attach(module.getExportByName('NRtClient_joinMatch'), {
        onEnter(args) {
            if (localSettings) send({ event: 'game_join_match', match_id: args[1].readUtf8String() });
        }
    });
    Interceptor.attach(module.getExportByName('NRtClient_sendMatchData'), {
        onEnter(args) {
            if (!localSettings) return;
            const data = args[3];
            const length = data.add(8).readU32();
            send({ event: 'game_match_data_tx', match_id: args[1].readUtf8String(),
                opcode: args[2].toInt32(), size: length }, data.readPointer().readByteArray(length));
        }
    });
    send({ event: 'nakama_hooks_installed' });
}

function installProfile(module) {
    if (installed.has(module.name)) return;
    installed.add(module.name);
    const name = module.findExportByName('?GetName@ProfileResult@Model@SDK@PDX@@QEBAPEBDXZ');
    if (name !== null) {
        Interceptor.attach(name, {
            onLeave(value) {
                if (localSettings) value.replace(localName);
            }
        });
    }
}

Interceptor.attach(base.add(0x380aea0), {
    onLeave(value) {
        if (localSettings && status().connected) value.replace(ptr(1));
    }
});

installLocalFrontendGates(() => localSettings && status().connected);

Process.attachModuleObserver({
    onAdded(module) {
        if (module.name.toLowerCase() === 'nakama-cpp.dll') installNakama(module);
        if (module.name.toLowerCase() === 'pdxsdk.dll') installProfile(module);
    }
});

rpc.exports = {
    status,
    sessionProfile,
    retryLogin() {
        loginRequested = false;
        return status();
    },
    readStrings(rva, offsets) {
        return offsets.map(offset => cppString(base.add(rva).add(offset)));
    },
    inspectPointer(rva, count) {
        const address = base.add(rva).readPointer();
        return { address: address.toString(), words: Array.from({ length: count },
            (_, index) => address.add(index * 8).readPointer().toString()) };
    }
};
send({ event: 'local_bridge_loaded', base: base.toString() });
