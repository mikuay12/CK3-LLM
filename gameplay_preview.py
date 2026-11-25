"""Read-only native declaration previews for the CK3 multiplayer client."""

import argparse
import json
import struct
import re

from process_memory import ProcessMemory
from shadow_evaluator import ShadowEvaluator, DB_LOOKUP_RVA, DB_REGISTRY_LOCK_RVA


def declaration_preview(evaluator, actor, recipient, casus_belli, titles=(), claimant=0xFFFFFFFF):
    for value, maximum in ((actor, 0xFFFFFFFE), (recipient, 0xFFFFFFFE), (claimant, 0xFFFFFFFF)):
        if type(value) is not int or not 0 <= value <= maximum:
            raise ValueError("Character identifiers must be unsigned native references")
    if not isinstance(casus_belli, str) or not casus_belli or len(casus_belli) > 255:
        raise ValueError("A casus belli script name is required")
    if not isinstance(titles, (list, tuple)) or len(titles) > 100 or any(
            type(value) is not int or not 0 <= value < 0xFFFFFFFF for value in titles):
        raise ValueError("Titles must be a bounded list of native identifiers")
    vm = evaluator.vm
    vm.refresh_dynamic_state()
    allocations = []

    def alloc(size):
        address = vm.alloc(size, zero=True)
        allocations.append(address)
        return address

    try:
        data = alloc(0x338)
        vm.write_u64(data, evaluator.interaction_type_pointer("declare_war_interaction"))
        evaluator._init_top_scope(data + 8, actor)
        evaluator._init_top_scope(data + 0x170, actor)
        for offset, value in ((0x2D8, actor), (0x2DC, recipient), (0x2E0, 0xFFFFFFFF),
                              (0x2E4, 0xFFFFFFFF), (0x2E8, 0xFFFFFFFF), (0x2EC, actor)):
            vm.write_u32(data + offset, value)
        vm.write_u16(data + 0x2F0, evaluator._scope_type_id("boolean"))
        vm.write_u64(data + 0x2F8, 1)
        options = alloc(32)
        vm.write_u64(data + 0x300, options)
        vm.write_i32(data + 0x308, 32)
        vm.write_i32(data + 0x30C, 1)
        encoded = casus_belli.encode("utf-8") + b"\0"
        name = alloc(len(encoded))
        vm.write_bytes(name, encoded)
        vm.write_u32(vm.base + DB_REGISTRY_LOCK_RVA, 0)
        # CCasusBelliType reference reader 0x2680AA0 loads this type-info address.
        definition = vm.call(DB_LOOKUP_RVA, rcx=vm.base + 0x55DA828, rdx=name)
        if not definition:
            raise ValueError("Unknown casus belli")
        special = alloc(0x30)
        vm.write_u64(special, vm.base + 0x4540228)
        vm.write_u64(special + 8, definition)
        if titles:
            entries = alloc(4 * len(titles))
            vm.write_bytes(entries, struct.pack("<" + "I" * len(titles), *titles))
            vm.write_u64(special + 0x10, entries)
            vm.write_i32(special + 0x18, len(titles))
            vm.write_i32(special + 0x1C, len(titles))
        vm.write_u32(special + 0x28, claimant)
        vm.write_u64(data + 0x330, special)
        vm.call(0x3088D40, rcx=data, rdx=0)
        base_valid = bool(vm.call(0x308AA50, rcx=data) & 0xFF)
        reason = alloc(32)
        vm.write_u64(reason + 0x18, 15)
        can_send = bool(vm.call(0x308C320, rcx=data, rdx=reason) & 0xFF)
        length = vm.read_u64(reason + 0x10)
        capacity = vm.read_u64(reason + 0x18)
        text = None
        if 0 < length <= 65536:
            address = vm.read_u64(reason) if capacity >= 16 else reason
            raw = bytes(vm.uc.mem_read(address, length))
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = raw.decode("gb18030", errors="replace")
            text = re.sub(r"\x16\w+!|\x15(?:[A-Za-z_]+(?::[^\s\x15]+)?;?|!)", "", text)
            text = "\n".join(" ".join(line.split()) for line in text.splitlines()).strip()
        return {"actor": actor, "recipient": recipient, "casus_belli": casus_belli,
                "titles": list(titles), "claimant": claimant, "base_valid": base_valid,
                "can_send": can_send, "reason": text, "source": "native_shadow_unicorn"}
    finally:
        for address in reversed(allocations):
            vm.free(address)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--actor", type=int, required=True)
    parser.add_argument("--recipient", type=int, required=True)
    parser.add_argument("--casus-belli", required=True)
    parser.add_argument("--title", type=int, action="append", default=[])
    parser.add_argument("--claimant", type=int, default=0xFFFFFFFF)
    args = parser.parse_args()
    memory = ProcessMemory(args.pid)
    try:
        evaluator = ShadowEvaluator(memory.executable, memory_source=memory)
        print(json.dumps(declaration_preview(evaluator, args.actor, args.recipient,
                                            args.casus_belli, args.title, args.claimant), ensure_ascii=True))
    finally:
        memory.close()


if __name__ == "__main__":
    main()
