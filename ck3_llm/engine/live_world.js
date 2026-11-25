// Appended to live_memory.js after the Python executable-hash check.
// Requests run between in-game update iterations on the game's main thread.
const nativeWorldConfig = __NATIVE_WORLD_CONFIG__;
const indexModule = new CModule(__WORLD_INDEX_SOURCE__);
const indexWorld = new NativeFunction(indexModule.index_world, 'int',
    ['pointer', 'uint', 'uint', 'uint', 'int', 'pointer', 'uint', 'pointer']);
const findTokenKey = new NativeFunction(indexModule.find_token_key, 'int',
    ['pointer', 'pointer', 'uint', 'uint']);
const serializeWorld = new NativeFunction(base.add(0x25b6670), 'bool', ['pointer', 'pointer']);
const worldAllocator = base.add(0x54f2230);
const releaseWorldBuffer = new NativeFunction(worldAllocator.readPointer().add(16).readPointer(),
    'void', ['pointer', 'pointer', 'int']);
let pendingSnapshot = null;
let worldReaderClosed = false;

function nativeIndex(data, size, start, end, wrapped) {
    const errorPosition = Memory.alloc(4);
    const count = indexWorld(data, size, start, end, wrapped ? 1 : 0, ptr(0), 0, errorPosition);
    if (count < 0) throw new Error('Native world index error ' + count + ' at ' + errorPosition.readU32());
    const rows = Memory.alloc(Math.max(16, count * 16));
    const actual = indexWorld(data, size, start, end, wrapped ? 1 : 0, rows, count, errorPosition);
    if (actual !== count) throw new Error('Native world index changed during verification');
    return {start, end, wrapped, count, rows};
}

function indexSections(data, size) {
    const root = nativeIndex(data, size, 0, size, false);
    const groups = [root];
    for (const name of ['living', 'dead_unprunable', 'provinces', 'landed_titles']) {
        const row = findTokenKey(data, root.rows, root.count, nativeWorldConfig.tokens[name]);
        if (row < 0) continue;
        const address = root.rows.add(row * 16);
        const section = nativeIndex(data, size, address.add(8).readU32(), address.add(12).readU32(), true);
        groups.push(section);
        if (name === 'landed_titles') {
            const inner = findTokenKey(data, section.rows, section.count, nativeWorldConfig.tokens[name]);
            if (inner >= 0) {
                const titleRow = section.rows.add(inner * 16);
                groups.push(nativeIndex(data, size, titleRow.add(8).readU32(), titleRow.add(12).readU32(), true));
            }
        }
    }
    return groups;
}

function writeBuffer(path, data, size) {
    const file = new File(path, 'wb');
    try {
        for (let offset = 0; offset < size; offset += 1024 * 1024) {
            file.write(data.add(offset).readByteArray(Math.min(1024 * 1024, size - offset)));
        }
        file.flush();
    } finally {
        file.close();
    }
}

function worldBoundary(state) {
    const host = state.players.find(player => player.machineid === nativeWorldConfig.host_machineid);
    if (!host) throw new Error('The host player is unavailable');
    return state.players.map(player => [player.machineid, player.character_id])
        .sort((left, right) => left[0] - right[0]);
}

function worldClock() {
    const synchronization = base.add(0x5c7f410).readPointer();
    const world = base.add(0x5c7dbe8).readPointer();
    if (synchronization.isNull() || world.isNull()) throw new Error('The world clock is unavailable');
    return {tick: synchronization.add(0x68).readU32(), date_raw: world.add(8).readU32()};
}

function captureWorld(request) {
    const before = players();
    if (!before.in_game) throw new Error('An active world is required');
    const clockBefore = worldClock();
    const manager = base.add(0x5ccd808).readPointer();
    const saveManager = manager.add(0x1c0).readPointer();
    if (saveManager.isNull() || saveManager.add(0x29).readU8()) {
        return {retry: 'save_in_progress'};
    }
    const adapterRoot = base.add(0x5c7f4b8).readPointer();
    if (adapterRoot.isNull()) throw new Error('World adapter is unavailable');
    const adapter = adapterRoot.add(8).readPointer();
    if (adapter.isNull() || !adapter.readPointer().add(8).readPointer().equals(base.add(0x25b6670))) {
        throw new Error('Unexpected native world adapter');
    }
    const output = Memory.alloc(24);
    output.writeByteArray(new Uint8Array(24));
    output.add(16).writePointer(worldAllocator);
    const started = Date.now();
    try {
        if (!serializeWorld(adapter, output)) throw new Error('Native world serialization failed');
        const serializedMs = Date.now() - started;
        const data = output.readPointer();
        const size = output.add(12).readU32();
        const capacity = output.add(8).readU32();
        if (data.isNull() || size < 30 || size > capacity || size > nativeWorldConfig.max_bytes) {
            throw new Error('Invalid native world buffer size');
        }
        const prefix = data.readUtf8String(24);
        if (!prefix.startsWith('SAV') || !prefix.includes('\n')) throw new Error('Unexpected native world header');
        const headerSize = prefix.indexOf('\n') + 1;
        const groups = indexSections(data.add(headerSize), size - headerSize);
        const after = players();
        const clockAfter = worldClock();
        if (JSON.stringify(clockBefore) !== JSON.stringify(clockAfter) ||
                JSON.stringify(worldBoundary(before)) !== JSON.stringify(worldBoundary(after))) {
            return {retry: 'world_boundary_changed'};
        }
        if (saveManager.add(0x29).readU8()) throw new Error('Native save context did not finish');
        writeBuffer(request.world_path, data, size);
        const indexFile = new File(request.index_path, 'wb');
        try {
            for (const group of groups) {
                if (group.count) indexFile.write(group.rows.readByteArray(group.count * 16));
            }
            indexFile.flush();
        } finally {
            indexFile.close();
        }
        return {
            source: 'live_host_memory', method: 'native_hotjoin_serializer', pid: Process.id,
            thread_id: Process.getCurrentThreadId(), captured_at: new Date(started).toISOString(),
            completed_at: new Date().toISOString(), serialization_ms: serializedMs,
            capture_ms: Date.now() - started, bytes: size, players_before: before.players,
            players_after: after.players, ...clockBefore, tick_source: 'game_synchronization_manager',
            clock_before: clockBefore, clock_after: clockAfter,
            index_groups: groups.map(({start, end, wrapped, count}) =>
                ({start, end, wrapped, count})),
        };
    } finally {
        const data = output.readPointer();
        if (!data.isNull()) {
            output.add(12).writeU32(0);
            releaseWorldBuffer(worldAllocator, data, 1);
            output.writePointer(ptr(0));
            output.add(8).writeU32(0);
        }
    }
}

const inGameUpdateHook = Interceptor.attach(base.add(0x25b4f60), {
    onEnter() {
        const pending = pendingSnapshot;
        if (pending === null) return;
        pendingSnapshot = null;
        clearTimeout(pending.timer);
        try {
            const result = captureWorld(pending.request);
            if (result.retry) {
                pending.attempts++;
                if (Date.now() >= pending.deadline) {
                    throw new Error('World capture could not reach a stable boundary: ' + result.retry);
                }
                pendingSnapshot = pending;
                pending.timer = setTimeout(() => {
                    if (pendingSnapshot === pending) pendingSnapshot = null;
                    pending.reject(new Error('World capture timed out while waiting for a stable boundary'));
                }, pending.deadline - Date.now());
                return;
            }
            pending.resolve({...result, capture_attempts: pending.attempts});
        } catch (error) {
            pending.reject(error);
        }
    },
});

rpc.exports.snapshot = function (request) {
    if (worldReaderClosed) throw new Error('The live world reader has closed');
    if (pendingSnapshot !== null) throw new Error('Another world request is pending');
    return new Promise((resolve, reject) => {
        const pending = {request, resolve, reject, timer: null, attempts: 1,
                         deadline: Date.now() + nativeWorldConfig.timeout_ms};
        pending.timer = setTimeout(() => {
            if (pendingSnapshot === pending) pendingSnapshot = null;
            reject(new Error('The in-game update did not service the world request'));
        }, nativeWorldConfig.timeout_ms);
        pendingSnapshot = pending;
    });
};

rpc.exports.clock = worldClock;

rpc.exports.shutdown = function () {
    worldReaderClosed = true;
    if (pendingSnapshot !== null) {
        clearTimeout(pendingSnapshot.timer);
        pendingSnapshot.reject(new Error('The live world reader has closed'));
        pendingSnapshot = null;
    }
    inGameUpdateHook.detach();
    Interceptor.flush();
    // Let an already-entered game frame leave its instrumentation trampoline.
    return new Promise(resolve => setTimeout(() => {
        indexModule.dispose();
        resolve(true);
    }, 250));
};
