use ck3_llm::protocol::{Envelope, Magic, Message, decode};

// Both vectors come from the Python reference encoder's synthetic self-test.
const DIRECTED: &str = "feca02004f000000726563697069656e74000000000000000000000000000000000000000000000000000000000000000c0000000500000001000000000102030b0000000500000000000000ff004140000000000000007b00000000000000736f757263652d757365720000000000000000000000000000000000000000000000000000000000000000000000000000";
const ALTERNATE: &str = "a5bc02004f000000000000000000000000000000000000000000000000000000000000000000000000000000000000000c0000000500000001000000000102030b0000000500000000000000ff004140000000000000007b00000000000000736f757263652d757365720000000000000000000000000000000000000000000000000000000000000000000000000000";

fn unhex(text: &str) -> Vec<u8> {
    let (pairs, remainder) = text.as_bytes().as_chunks::<2>();
    assert!(remainder.is_empty());
    pairs
        .iter()
        .map(|pair| u8::from_str_radix(str::from_utf8(pair).unwrap(), 16).unwrap())
        .collect()
}

#[test]
fn matches_python_reference_for_both_frame_types() {
    for (magic, expected, recipient) in [
        (Magic::Directed, DIRECTED, "recipient"),
        (Magic::Alternate, ALTERNATE, ""),
    ] {
        let mut envelope = Envelope {
            magic,
            recipient: "recipient",
            sequence: 123,
            source_user: "source-user",
            messages: vec![
                Message {
                    word_4: 5,
                    word_8: 1,
                    payload: &[0, 1, 2, 3],
                },
                Message {
                    word_4: 5,
                    word_8: 0,
                    payload: &[255, 0, 65],
                },
            ],
        };
        let wire = envelope.encode().unwrap();
        assert_eq!(wire, unhex(expected));
        envelope.recipient = recipient;
        assert_eq!(decode(&wire).unwrap(), envelope);
    }
}

#[test]
fn rejects_corrupt_boundaries_count_and_identifiers() {
    let wire = unhex(DIRECTED);
    for end in 0..wire.len() {
        assert!(decode(&wire[..end]).is_err(), "accepted {end} bytes");
    }
    let mut invalid = wire.clone();
    invalid[48..52].copy_from_slice(&u32::MAX.to_le_bytes());
    assert!(decode(&invalid).is_err());
    let mut invalid = wire.clone();
    invalid[2] = 1;
    assert!(decode(&invalid).is_err());
    let mut invalid = wire;
    invalid[8] = 0xff;
    assert!(decode(&invalid).is_err());
}

#[test]
fn handles_frame_and_identifier_limits() {
    let payload = vec![1; (1 << 20) - 48 - 12 + 1];
    let mut envelope = Envelope {
        magic: Magic::Directed,
        recipient: "",
        sequence: u64::MAX,
        source_user: "012345678901234567890123456789012345678901234567",
        messages: vec![Message {
            word_4: u32::MAX,
            word_8: 0,
            payload: &payload[..payload.len() - 1],
        }],
    };
    assert_eq!(decode(&envelope.encode().unwrap()).unwrap(), envelope);
    envelope.messages[0].payload = &payload;
    assert!(envelope.encode().is_err());
    envelope.messages.clear();
    envelope.recipient = "\0";
    assert!(envelope.encode().is_err());
}
