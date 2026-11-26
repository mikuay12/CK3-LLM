use super::*;

const BASE: u64 = 0x7ff6_0000_0000;
const OBJECT: u64 = 0x100_0000;

struct Fixture;

impl Memory for Fixture {
    fn read(&self, address: u64, buffer: &mut [u8]) -> Result<()> {
        buffer.fill(0);
        if address == BASE + PAGE {
            // mov qword ptr [rcx], rdx; mov rax, [rcx]; ret
            buffer[..7].copy_from_slice(&[0x48, 0x89, 0x11, 0x48, 0x8b, 0x01, 0xc3]);
        }
        Ok(())
    }
}

#[test]
fn native_evaluator_supports_aslr_and_keeps_writes_in_the_emulator() {
    if std::env::var_os("CK3_UNICORN_DLL").is_none() {
        return;
    }
    let memory = Fixture;
    let mut evaluator = Shadow::new(&memory, BASE).unwrap();
    assert_eq!(evaluator.call(PAGE, &[OBJECT, 42]).unwrap(), 42);
    assert_eq!(u64::from_le_bytes(evaluator.read(OBJECT).unwrap()), 42);
    let mut live = [0; 8];
    memory.read(OBJECT, &mut live).unwrap();
    assert_eq!(live, [0; 8]);
}
