// Read-only CK3 1.20.1.2 probe. The Python caller verifies the executable hash.
// Every pointer chain and character reference is checked before returning data.
const base = Process.mainModule.base;

function cppString(address) {
    const length = address.add(0x10).readU64().toNumber();
    const capacity = address.add(0x18).readU64().toNumber();
    if (length < 0 || length > 4096 || length > capacity) {
        throw new Error('Invalid observed C++ string');
    }
    const data = capacity < 16 ? address : address.readPointer();
    if (data.isNull()) throw new Error('Observed string pointer is null');
    return length ? data.readUtf8String(length) : '';
}

function players() {
    const manager = base.add(0x5ccd808).readPointer();
    if (manager.isNull()) return { in_game: false, players: [] };
    const count = manager.add(0x1e4).readS32();
    if (count < 0 || count > 64) throw new Error('Invalid observed player count');
    const array = manager.add(0x1d8).readPointer();
    if (count && array.isNull()) throw new Error('Observed player array is null');
    const result = [];
    for (let index = 0; index < count; index++) {
        const player = array.add(index * 8).readPointer();
        if (player.isNull()) throw new Error('Observed player pointer is null');
        result.push({
            name: cppString(player.add(8)),
            machineid: player.add(0x70).readS32(),
            character_id: player.add(0x58).readU32(),
            flags: player.add(0x48).readU8(),
            turn: player.add(0x74).readU32(),
        });
    }
    return { in_game: count > 0, players: result };
}

function characterFocus(id) {
    if (!Number.isInteger(id) || id < 0 || id >= 0xffffffff) {
        throw new Error('Character ID must be an unsigned integer');
    }
    const database = base.add(0x5c7c4f0).readPointer();
    if (database.isNull()) throw new Error('Character database is unavailable');
    const slots = database.add(0x20).readPointer();
    const length = database.add(0x2c).readU32();
    const index = id & 0xffffff;
    if (slots.isNull() || index >= length) throw new Error('Character index is out of range');
    const character = slots.add(index * 16 + 8).readPointer();
    if (character.isNull() || character.add(0x18).readU32() !== id) {
        throw new Error('Character reference does not match this ID');
    }
    const aliveData = character.add(0x1b0).readPointer();
    if (aliveData.isNull()) return { id, alive: false, focus_type: null, health: null };
    const focus = aliveData.add(0x280).readPointer();
    const health = aliveData.add(0x2e8).readS64().toNumber() / 100000;
    let focusType = null;
    if (!focus.isNull()) {
        const target = focus.add(8).readPointer();
        if (!target.isNull()) focusType = cppString(target.add(0x18));
    }
    return { id, alive: true, focus_type: focusType, health };
}

function controlledCharacter(machineid) {
    if (!Number.isInteger(machineid) || machineid < 0 || machineid > 63) {
        throw new Error('Invalid player machine ID');
    }
    for (let attempt = 0; attempt < 3; attempt++) {
        const before = players().players.find(player => player.machineid === machineid);
        if (!before) throw new Error('Player is not in the current game');
        const character = before.character_id === 0xffffffff ? null : characterFocus(before.character_id);
        const after = players().players.find(player => player.machineid === machineid);
        if (after && after.character_id === before.character_id && after.turn === before.turn) {
            return { player: after, character: character };
        }
    }
    throw new Error('Game state advanced during the live query; retry');
}

rpc.exports = {
    inspect(request) {
        if (!request || typeof request !== 'object') throw new Error('Query must be an object');
        if (request.kind === 'players') return players();
        if (request.kind === 'character_focus') return characterFocus(request.id);
        if (request.kind === 'controlled_character') return controlledCharacter(request.machineid === undefined ? 1 : request.machineid);
        throw new Error('Supported live queries: players, character_focus, controlled_character');
    },
};
