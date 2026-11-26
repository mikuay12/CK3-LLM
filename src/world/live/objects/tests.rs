use std::cell::Cell;

use super::*;

const BASE: u64 = 0x140000000;
const STATE: u64 = 0x1000;
const DATA: u64 = 0x2000;
const SYNC: u64 = 0x3000;
const PERSON: u64 = 0xa000;
const ALIVE: u64 = 0xb000;
const LANDED: u64 = 0xc000;
const BARONY: u64 = 0xe000;
const DEFINITION: u64 = 0xf000;
const PROVINCE: u64 = 0x12000;
const COUNTY: u64 = 0x14000;
const CHARACTER_ID: u32 = 0x01000001;
const TITLE_ID: u32 = 0x02000002;
const DATE: u32 = (5000 + 1066) * 365 * 24;

#[derive(Default)]
struct Fixture {
    bytes: HashMap<u64, u8>,
    tick: Cell<u32>,
    advance_on_character: Cell<bool>,
    replace_reference_on_validation: Cell<bool>,
    reference_replaced: Cell<bool>,
}

impl Fixture {
    fn set(&mut self, address: u64, bytes: &[u8]) {
        self.bytes.extend((address..).zip(bytes.iter().copied()));
    }

    fn u32(&mut self, address: u64, value: u32) {
        self.set(address, &value.to_le_bytes());
    }

    fn ptr(&mut self, address: u64, value: u64) {
        self.set(address, &value.to_le_bytes());
    }

    fn inline(&mut self, address: u64, value: &str) {
        assert!(value.len() < 16);
        self.set(address, value.as_bytes());
        self.ptr(address + 0x10, value.len() as u64);
        self.ptr(address + 0x18, 15);
    }

    fn object(&mut self, layout: Layout, id: u32, database: u64, slots: u64, object: u64) {
        self.ptr(BASE + layout.global, database);
        self.ptr(database + 0x20, slots);
        self.u32(database + 0x2c, 4);
        self.ptr(slots + u64::from(id & 0xffffff) * SLOT_BYTES + 8, object);
        self.ptr(object, BASE + layout.vtable);
        self.u32(object + layout.id_offset, id);
    }

    fn campaign() -> Self {
        let mut memory = Self::default();
        memory.ptr(BASE + WORLD, STATE);
        memory.ptr(BASE + SYNCHRONIZATION, SYNC);
        memory.ptr(STATE + 0xa0, DATA);
        memory.u32(STATE + 8, DATE);
        memory.tick.set(12);
        memory.ptr(BASE + PLAYER_MANAGER, 0x5000);
        memory.ptr(0x5000 + 0x1d8, 0x6000);
        memory.u32(0x5000 + 0x1e4, 1);
        memory.ptr(0x6000, 0x7000);
        memory.inline(0x7000 + 8, "Local player");
        memory.u32(0x7000 + 0x58, CHARACTER_ID);

        memory.object(CHARACTER, CHARACTER_ID, 0x8000, 0x9000, PERSON);
        memory.inline(PERSON + 0x20, "Ruler");
        memory.set(PERSON + 0x68, &24i16.to_le_bytes());
        memory.u32(PERSON + 0x60, DATE - 24 * 365 * 24);
        memory.u32(PERSON + 0xb0, NO_REFERENCE);
        memory.u32(PERSON + 0xb4, NO_REFERENCE);
        memory.u32(PERSON + 0x158, NO_REFERENCE);
        memory.ptr(PERSON + 0x1b0, ALIVE);
        memory.ptr(PERSON + 0x1c0, LANDED);
        memory.ptr(ALIVE + 0x100, 12_500_000);
        memory.ptr(LANDED + 0x1e0, 0x18000);
        memory.u32(LANDED + 0x1e8, 1);
        memory.u32(LANDED + 0x1ec, 1);
        memory.u32(0x18000, TITLE_ID);
        memory.u32(LANDED + 0x1f8, TITLE_ID);

        memory.object(TITLE, TITLE_ID, 0x16000, 0x17000, BARONY);
        memory.ptr(BARONY + 0x48, DEFINITION);
        for offset in [0xe4, 0xe8, 0x108, 0x12c] {
            memory.u32(BARONY + offset, NO_REFERENCE);
        }
        memory.u32(BARONY + 0x128, CHARACTER_ID);
        memory.inline(DEFINITION + 0x18, "b_test");
        memory.u32(DEFINITION + 0x38, 0x4744624f);
        memory.u32(DEFINITION + 0x64, 1);

        let manager = DATA + 0x118;
        memory.ptr(manager, BASE + PROVINCE_MANAGER_VTABLE);
        memory.ptr(manager + 0x28, 0x11000);
        memory.u32(manager + 0x34, 2);
        memory.ptr(0x11000 + 8, PROVINCE);
        memory.ptr(PROVINCE, BASE + PROVINCE_VTABLE);
        memory.u32(PROVINCE + 0x10, 1);
        memory.u32(PROVINCE + 0x738, TITLE_ID);
        memory.u32(PROVINCE + 0x73c, NO_REFERENCE);
        memory.ptr(PROVINCE + 0x848, COUNTY);
        memory.u32(PROVINCE + 0x850, 5);
        memory.ptr(COUNTY, BASE + COUNTY_DATA_VTABLE);
        memory.u32(COUNTY + 0x384, NO_REFERENCE);
        memory.u32(COUNTY + 0x388, NO_REFERENCE);
        memory.u32(COUNTY + 0x38c, 10);
        memory
    }
}

impl Memory for Fixture {
    fn read(&self, address: u64, buffer: &mut [u8]) -> Result<()> {
        if address == PERSON && self.advance_on_character.replace(false) {
            self.tick.set(self.tick.get() + 1);
        }
        if address == 0x9000 + SLOT_BYTES + 8 && self.replace_reference_on_validation.replace(false)
        {
            self.reference_replaced.set(true);
        }
        if address == SYNC + 0x68 {
            buffer.copy_from_slice(&self.tick.get().to_le_bytes());
        } else {
            for (address, byte) in (address..).zip(buffer.iter_mut()) {
                *byte = if self.reference_replaced.get()
                    && (PERSON + 0x18..PERSON + 0x1c).contains(&address)
                {
                    (CHARACTER_ID + (1 << 24)).to_le_bytes()[(address - PERSON - 0x18) as usize]
                } else {
                    self.bytes.get(&address).copied().unwrap_or(0)
                };
            }
        }
        Ok(())
    }
}

#[test]
fn live_queries_refresh_fields_and_validate_title_links() {
    let mut memory = Fixture::campaign();
    let mut reader = Catalogs::default();
    let summary = reader.query(&memory, BASE, Query::Summary).unwrap();
    assert_eq!(summary["date"], "1066.1.1");
    assert_eq!(summary["data"]["players"][0]["character_id"], CHARACTER_ID);
    let character = reader
        .query(&memory, BASE, Query::Character(CHARACTER_ID))
        .unwrap();
    assert_eq!(character["data"]["identity"]["age"], 24);
    assert_eq!(character["data"]["identity"]["birth"], "1042.1.1");
    assert_eq!(character["data"]["resources"]["gold"], 125.0);
    assert_eq!(character["data"]["controller"]["type"], "player");

    let title = reader.query(&memory, BASE, Query::Title("b_test")).unwrap();
    assert_eq!(title["data"]["id"], TITLE_ID);
    assert_eq!(title["data"]["province_id"], 1);
    let province = reader.query(&memory, BASE, Query::Province(1)).unwrap();
    assert_eq!(province["data"]["county"]["development"], 10);
    assert_eq!(province["data"]["realm"]["ruler"]["id"], CHARACTER_ID);

    memory.inline(DEFINITION + 0x18, "b_new");
    memory.ptr(ALIVE + 0x100, 13_125_000);
    memory.u32(STATE + 8, DATE + 24);
    memory.tick.set(13);
    let character = reader
        .query(&memory, BASE, Query::Character(CHARACTER_ID))
        .unwrap();
    assert_eq!(character["data"]["resources"]["gold"], 131.25);
    assert_eq!(character["date"], "1066.1.2");
    assert!(reader.query(&memory, BASE, Query::Title("b_test")).is_err());
    assert_eq!(
        reader.query(&memory, BASE, Query::Title("b_new")).unwrap()["data"]["id"],
        TITLE_ID
    );
}

#[test]
fn live_query_retries_on_tick_change_and_rejects_recycled_reference() {
    let memory = Fixture::campaign();
    let mut reader = Catalogs::default();
    memory.advance_on_character.set(true);
    let character = reader
        .query(&memory, BASE, Query::Character(CHARACTER_ID))
        .unwrap();
    assert_eq!(character["tick"], 13);
    memory.replace_reference_on_validation.set(true);
    assert!(
        reader
            .query(&memory, BASE, Query::Character(CHARACTER_ID))
            .is_err()
    );
    assert!(memory.reference_replaced.get());
}
