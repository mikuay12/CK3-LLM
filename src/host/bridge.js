// CK3 1.20.1.2 runtime adapter. The launcher verifies the executable hash.
const base = Process.mainModule.base;
const config = localHostConfig;
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

function emit(event, fields = {}) {
    send({ type: 'ck3:host', id: 0, result: 'ok', returns: { event, ...fields } });
}

function wrapper() {
    const value = base.add(0x5cd1e78).readPointer();
    return value.isNull() ? null : value;
}

function status() {
    const value = wrapper();
    if (value === null) return { initialized: false, connected: false };
    const implementation = value.readPointer();
    if (implementation.isNull()) return { initialized: false, connected: false };
    const realtime = implementation.add(0x20).readPointer();
    let connected = false;
    if (!realtime.isNull() && rtConnected !== null) {
        const handle = realtime.add(0x10).readPointer();
        connected = !handle.isNull() && !!rtConnected(handle);
    }
    return { initialized: true, auth_state: implementation.add(0x2e0).readU32(), connected };
}

function cppString(address, decodeText = true) {
    const length = address.add(0x10).readU64().toNumber();
    const capacity = address.add(0x18).readU64().toNumber();
    if (length > 65535 || length > capacity) throw new Error('Invalid bounded CK3 string');
    const data = capacity < 16 ? address : address.readPointer();
    return {
        length,
        text: decodeText ? data.readUtf8String(length) : null,
        hex: Array.from(new Uint8Array(data.readByteArray(length)))
            .map(value => value.toString(16).padStart(2, '0')).join(''),
    };
}

function sessionProfile() {
    const value = wrapper();
    if (!localSettings || value === null) return null;
    const implementation = value.readPointer();
    if (implementation.isNull()) return null;
    const match = cppString(implementation.add(0x1d8)).text;
    if (!match || match !== cppString(implementation.add(0x1b8)).text) return null;

    const session = base.add(0x5cd6250);
    const compatibility = [0x1e8, 0x208, 0x228].map(offset => cppString(session.add(offset), false));
    if (compatibility[0].length !== 16 || compatibility[1].length !== 16 || compatibility[2].length > 33) return null;
    const hostUser = cppString(implementation.add(0x1f8)).text;
    if (!hostUser) return null;
    return {
        exe_sha256: config.exe_sha256, host: config.host, port: config.port, ssl: false,
        match_id: match, host_user_id: hostUser,
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
        const summary = JSON.stringify(current);
        if (summary !== lastStatus) {
            lastStatus = summary;
            emit('local_game_status', current);
        }
        if (current.connected && Date.now() >= nextProfileCheck) {
            nextProfileCheck = Date.now() + 1000;
            const profile = sessionProfile();
            if (profile !== null && profile.match_id !== lastProfileMatch) {
                lastProfileMatch = profile.match_id;
                emit('local_session_profile', { profile });
            }
        }
        if (loginRequested || !current.initialized) return;
        loginRequested = true;
        const value = wrapper();
        const implementation = value.readPointer();
        assignString(implementation.add(0x2e8), localId, 14);
        assignString(implementation.add(0x308), localCredential, 13);
        beginLogin(value);
        emit('local_game_login_requested');
    } catch (error) {
        emit('local_bridge_error', { message: String(error) });
    }
}

function installNakama(module) {
    rtConnected = new NativeFunction(module.getExportByName('NRtClient_isConnected'), 'bool', ['pointer']);
    Interceptor.attach(module.getExportByName('createDefaultNakamaClient'), {
        onEnter(args) {
            const parameters = args[0];
            const host = parameters.add(8).readPointer().readUtf8String();
            const port = parameters.add(16).readU32();
            const ssl = parameters.add(20).readU8() !== 0;
            localSettings = host === config.host && port === config.port && !ssl;
            emit('game_backend_settings', { host, port, ssl, local_settings: localSettings });
        },
    });
    Interceptor.attach(module.getExportByName('NClient_tick'), { onEnter: onTick });
}

function installLocalName(module) {
    const getName = module.findExportByName('?GetName@ProfileResult@Model@SDK@PDX@@QEBAPEBDXZ');
    if (getName === null) return;
    Interceptor.attach(getName, {
        onLeave(value) {
            if (localSettings) value.replace(localName);
        },
    });
}

Interceptor.attach(base.add(0x380aea0), {
    onLeave(value) {
        if (localSettings && status().connected) value.replace(ptr(1));
    },
});

for (const rva of [0x18923f2, 0x189293a]) {
    Interceptor.attach(base.add(rva), {
        onEnter() {
            if (localSettings && status().connected) {
                this.context.rbx = this.context.rbx.and(ptr('0xffffffffffffff00'));
            }
        },
    });
}

Process.attachModuleObserver({
    onAdded(module) {
        const name = module.name.toLowerCase();
        if (installed.has(name)) return;
        installed.add(name);
        if (name === 'nakama-cpp.dll') installNakama(module);
        if (name === 'pdxsdk.dll') installLocalName(module);
    },
});
