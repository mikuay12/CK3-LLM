"""CK3 1.20.1.2 command serialization recovered from its Windows executable.

Supports confirmed integer, boolean, string, binary-data and object forms.
Known command encoders cover player synchronization, selected gameplay actions
and serial/fast file transfers. Live tests confirmed player registration,
serial snapshot reception, character selection and ready-state execution.
"""

import argparse
import json
import math
from pathlib import Path
import struct

from extract_jomini_tokens import SUPPORTED_EXE_SHA256
from session_codec import (
    CLIENT_SESSION_TYPES, SERVER_SESSION_TYPES, SESSION_SCHEMAS,
    decode_session_message, encode_session_message,
)


DEFAULT_TOKENS = Path(__file__).with_name("jomini_tokens.json")
EQUAL = 1
OPEN = 3
CLOSE = 4
I32 = 0x0C
F32 = 0x0D
BOOL = 0x0E
STRING = 0x0F
U32 = 0x14
UNQUOTED_STRING = 0x17
FIXED_POINT = 0x167
BLOB = 0x1FC
RGB = 0x243
U64 = 0x29C
U32_LIST = "u32_list"
I32_LIST = "i32_list"
STRING_LIST = "string_list"
U8_LIST = "u8_list"
BOOLEAN_PARAMETERS = "boolean_event_target_parameters"
SCOPE_VALUE = "scope_value"
TOKEN_VALUE = "token_value"
I32_ENUM = "i32_enum"
RALLY_POINTS = "rally_points"
INTERACTION_DATA = "declare_war_interaction_data"
ACTIVITY_INTENT = "activity_intent"
TRAVEL_PLAN = "travel_plan"
ACTIVITY_PLAN = "activity_plan"
SIMPLE_INTERACTIONS = ("arrange_marriage_interaction", "gift_interaction",
                       "offer_vassalization_interaction", "invite_to_court_interaction")
DEFAULT_INTERACTIONS_DIR = Path(r"C:\Games\Crusader Kings III\game\common\character_interactions")
_SIMPLE_INTERACTIONS_CACHE = None


_INTERACTION_SPECS_CACHE = None

PLAIN_SPECIAL_INTERACTIONS = frozenset({
    "call_ally_interaction",
    "interfere_in_war_interaction",
    "feudalize_holding",
    "tribalize_holding",
    "retract_vassal_interaction",
    "grant_vassal_interaction",
    "request_conversion_interaction",
    "offer_peace_interaction",
    "force_join_faction",
    "legitimize_bastard",
    "invite_to_scheme_interaction",
    "break_betrothal_interaction",
    "ransom_interaction",
    "release_from_prison_interaction",
    "execute_prisoner_interaction",
    "remove_scheme_interaction",
    "rite_conversion",
    "reform_faith",
    "request_great_project_contribution",
})

CULTURE_TRAIT_PILLAR_TOKENS = (
    ("ethos", 0x352D),
    ("heritage", 0x352F),
    ("language", 0x00BB),
    ("martial_custom", 0x36AD),
    ("head_determination", 0x3D57),
)

SPECIAL_INTERACTION_SPECS = {
    "arrange_marriage_interaction": {"token": 0x2BE4, "fields": []},
    "end_war_attacker_victory_interaction": {"token": 0x2B43, "fields": []},
    "end_war_white_peace_interaction": {"token": 0x2B44, "fields": []},
    "end_war_attacker_defeat_interaction": {"token": 0x2B45, "fields": []},
    "force_vote_in_succession_election": {"token": 0x329C, "fields": []},
    "grant_titles_interaction": {
        "token": 0x2C76,
        "fields": [("titles", 0x27D5, U32_LIST, []), ("government", 0x2EF7, STRING, None)],
    },
    "grant_governorship_interaction": {
        "token": 0x3955,
        "fields": [("titles", 0x27D5, U32_LIST, []), ("government", 0x2EF7, STRING, None)],
    },
    "revoke_title_interaction": {
        "token": 0x2E72,
        "fields": [("title", 0x6CE, U32, None)],
    },
    "diarch_revoke_title": {
        "token": 0x3735,
        "fields": [("title", 0x6CE, U32, None)],
    },
    "create_claimant_faction_against_interaction": {
        "token": 0x2F6D,
        "fields": [("title", 0x6CE, U32, 0xFFFFFFFF)],
    },
    "create_ceremonial_claimant_faction_against_interaction": {
        "token": 0x3DFA,
        "fields": [("title", 0x6CE, U32, 0xFFFFFFFF)],
    },
    "invite_to_council_interaction": {
        "token": 0x315E,
        "fields": [("active_council_task", 0x2E2B, U32, 0xFFFFFFFF)],
    },
    "force_onto_council_interaction": {
        "token": 0x32BA,
        "fields": [("active_council_task", 0x2E2B, U32, 0xFFFFFFFF)],
    },
    "request_council_position": {
        "token": 0x395F,
        "fields": [("active_council_task", 0x2E2B, U32, 0xFFFFFFFF)],
    },
    "grant_court_position": {
        "token": 0x3CDD,
        "fields": [("court_position", 0x3688, STRING, None)],
    },
    "request_court_position": {
        "token": 0x3CDB,
        "fields": [("court_position", 0x3688, STRING, None)],
    },
    "liege_modify_vassal_contract": {
        "token": 0x3B91,
        "fields": [("types", 0x37B, STRING_LIST, []), ("level", 0x753, U8_LIST, [])],
    },
    "vassal_modify_vassal_contract": {
        "token": 0x3B90,
        "fields": [("types", 0x37B, STRING_LIST, []), ("level", 0x753, U8_LIST, [])],
    },
    "migration": {
        "token": 0x3C66,
        "fields": [
            ("target", 0x6B, U32, None),
            ("domain", 0x27E6, U32, None),
            ("recipient", 0x28AB, U32, None),
            ("defenders", 0x2FA3, U32_LIST, []),
        ],
    },
    "request_culture_conversion_interaction": {
        "token": 0x353B,
        "fields": [("traits", 0x648, "culture_traits", {})],
    },
}


def supported_interaction_specs(directory=None):
    """Return installed non-debug interactions and their native target/special payload metadata."""
    global _INTERACTION_SPECS_CACHE
    target_dir = DEFAULT_INTERACTIONS_DIR if directory is None else Path(directory)
    if directory is None and _INTERACTION_SPECS_CACHE is not None:
        return dict(_INTERACTION_SPECS_CACHE)
    if not target_dir.is_dir():
        result = {
            name: {
                "target_type": None,
                "special_interaction": name if name == "arrange_marriage_interaction" else None,
            }
            for name in SIMPLE_INTERACTIONS
        }
    else:
        from event_catalog import script_fields
        from notification_catalog import definitions

        result = {}
        for path in sorted(target_dir.glob("*.txt")):
            stem = path.stem.lower()
            if "debug" in stem or "test" in stem:
                continue
            try:
                rows = definitions(path, include_body=True)
            except (OSError, ValueError):
                continue
            for row in rows:
                name = row["key"]
                fields = script_fields(row["body"])
                fmap = {item["key"]: item["value"] for item in fields if not item["block"]}
                cat = fmap.get("category")
                if cat and "debug" in str(cat).lower():
                    continue
                special = fmap.get("special_interaction")
                if special == "declare_war_interaction":
                    continue
                if special is not None and special not in SPECIAL_INTERACTION_SPECS and special not in PLAIN_SPECIAL_INTERACTIONS:
                    continue
                target_type = fmap.get("target_type")
                if target_type not in (None, "title", "artifact", "court_position_type"):
                    continue
                result[name] = {
                    "target_type": target_type,
                    "special_interaction": special,
                }
        if not result:
            result = {
                name: {
                    "target_type": None,
                    "special_interaction": name if name == "arrange_marriage_interaction" else None,
                }
                for name in SIMPLE_INTERACTIONS
            }
    if directory is None:
        _INTERACTION_SPECS_CACHE = dict(result)
    return result


def supported_simple_interactions(directory=None):
    """Return installed non-debug character interactions mapped to their script target_type."""
    return {name: meta["target_type"] for name, meta in supported_interaction_specs(directory).items()}
INTERACTION_REPLIES = {"accept": 0, "reject": 1, "block": 2, "acknowledge": 4}
TRAVEL_COMPANION_SOURCES = {
    "invalid": 0x4FC, "travel_leader": 0x3787, "travel_option": 0x37D2,
    "activity": 0x2816, "activity_option": 0x37D3,
}
TRAVEL_ON_ARRIVAL_DESTINATIONS = {
    "all": 0x465, "first": 0x277F, "last": 0x2C91, "all_but_last": 0x3A0C, "invalid": 0x4FC,
}
LIST_TYPES = {U32_LIST: U32, I32_LIST: I32, STRING_LIST: STRING, U8_LIST: U32}
MAX_MESSAGE_SIZE = 1 << 20
VALID_FOR = {"none": 0x165, "player": 0x274A, "ai": 0x2ADF}
COST_CHOICE = {"yes": 0x40E, "no": 0x40F}
KNIGHT_STATUS_VALUES = {"force": 0x19E, "default": 0x346, "not": 0x4F8, "none": 0x165}
MASS_PRISONER_INTERACTIONS = {
    "ransom_interaction": 0x2CBD,
    "release_from_prison_interaction": 0x2CC7,
    "execute_prisoner_interaction": 0x2E63,
}

TRANSFER_FIELDS = [
    ("filename", 0x3B6, STRING, None), ("size", 0x2F, U32, None),
    ("checksum", 0x179, I32, None),
]

# field token, binary value type, optional default
COMMAND_SCHEMAS = {
    "filetransfer_init_command": {
        "token": 0x6A0, "vtable_rva": 0x492F248,
        "write_rva": 0x368BAB0, "read_rva": 0x368BBA0,
        "fields": TRANSFER_FIELDS,
    },
    "filetransfer_request_chunk_metas_command": {
        "token": 0x6A1, "vtable_rva": 0x4954D00,
        "write_rva": 0x3832F40, "read_rva": 0x3832F50,
        "fields": TRANSFER_FIELDS,
    },
    "filetransfer_send_chunk_metas_command": {
        "token": 0x6A2, "vtable_rva": 0x4954D98,
        "write_rva": 0x38334B0, "read_rva": 0x3833540,
        "fields": TRANSFER_FIELDS + [
            ("checksums", 0x6C7, I32_LIST, []), ("sizes", 0x6C8, U32_LIST, []),
            ("machineid", 0x16, I32, None),
        ],
    },
    "filetransfer_request_chunk_command": {
        "token": 0x6D6, "vtable_rva": 0x4954E30,
        "write_rva": 0x3833DF0, "read_rva": 0x3833E50,
        "fields": TRANSFER_FIELDS + [("chunk_id", 0x6CA, I32, None)],
    },
    "filetransfer_send_chunk_command": {
        "token": 0x6D7, "vtable_rva": 0x4954EC8,
        "write_rva": 0x38341C0, "read_rva": 0x3834230,
        "fields": TRANSFER_FIELDS + [("chunk_id", 0x6CA, I32, None), ("data", 0xF0, BLOB, None)],
    },
    "filetransfer_chunk_recieved_command": {
        "token": 0x6D8, "vtable_rva": 0x4954F60,
        "write_rva": 0x38345F0, "read_rva": 0x3834700,
        "fields": TRANSFER_FIELDS + [
            ("machineid", 0x16, I32, None), ("chunk_id", 0x6CA, I32, None),
            ("chunks_completed", 0x6CB, U32, None), ("chunks_total", 0x6CC, U32, None),
        ],
    },
    "sync_players_command": {
        "token": 0x6A5, "vtable_rva": 0x492F4E0,
        "write_rva": 0x368D170, "read_rva": 0x368D1E0,
        "fields": [
            ("machineid", 0x16, U32_LIST, []), ("players", 0x6E2, STRING_LIST, []),
            ("character", 0x6EC, U32_LIST, []), ("flags", 0x6E4, U8_LIST, []),
        ],
    },
    "add_player": {
        "token": 0x69F, "vtable_rva": 0x492F1B0,
        "write_rva": 0x36893D0, "read_rva": 0x36894C0,
        "fields": [("name", 0x1B, STRING, None), ("machineid", 0x16, I32, None),
                   ("is_host", 0x50F, BOOL, None)],
    },
    "set_player_here_command": {
        "token": 0x2A9, "vtable_rva": 0x492F740,
        "write_rva": 0x368A5F0, "read_rva": 0x368A650,
        "fields": [("machineid", 0x16, I32, None)],
    },
    "set_observer_command": {
        "token": 0x5BF, "vtable_rva": 0x492FA38,
        "write_rva": 0x368E7B0, "read_rva": 0x368E840,
        "fields": [("machineid", 0x16, I32, None), ("flag", 0x384, BOOL, None)],
    },
    "add_perk": {
        "token": 0x3350, "vtable_rva": 0x4770FB8,
        "write_rva": 0x28981B0, "read_rva": 0x28982F0,
        "fields": [("target", 0x6B, U32, None), ("perk", 0x2965, STRING, None)],
    },
    "set_character_focus": {
        "token": 0x2C3C, "vtable_rva": 0x4771080,
        "write_rva": 0x2897BA0, "read_rva": 0x2897C90,
        "fields": [("target", 0x6B, U32, None), ("focus_type", 0x2C02, STRING, None),
                   ("actor", 0x28AA, U32, None)],
    },
    "execute_decision": {
        "token": 0x2E64, "vtable_rva": 0x4771C10,
        "write_rva": 0x289A310, "read_rva": 0x289A4B0,
        "encoding_scope": "Decisions with optional boolean event-target parameters",
        "optional_fields": ["parameters"],
        "fields": [("character", 0x6EC, U32, None), ("puppeteer", 0x3F16, U32, 0xFFFFFFFF),
                   ("decision", 0x2E65, STRING, None), ("parameters", 0x2FFA, BOOLEAN_PARAMETERS, None)],
    },
    "select_event_option": {
        "token": 0x27D4, "vtable_rva": 0x4784108,
        "write_rva": 0x29CAFA0, "read_rva": 0x29CB030,
        "fields": [("data", 0xF0, I32, None), ("option", 0x6CF, I32, None)],
    },
    "reply_character_interaction": {
        "token": 0x28B4, "vtable_rva": 0x449BD98,
        "write_rva": 0x29752D0, "read_rva": 0x2975360,
        "enums": {"accept": INTERACTION_REPLIES},
        "fields": [("character_interaction", 0x28A9, U32, None), ("accept", 0x28B0, I32_ENUM, None)],
    },
    "join_activity_command": {
        "token": 0x37DF, "vtable_rva": 0x477C680,
        "write_rva": 0x298A610, "read_rva": 0x298A770,
        "encoding_scope": "Activity and character references with native travel, start-date and optional travel plan draft",
        "optional_fields": ["travel_plan_draft_created"],
        "enums": {"travel": {"yes": 0x40E, "no": 0x40F}},
        "fields": [("activity", 0x2816, U32, 0xFFFFFFFF), ("character", 0x6EC, U32, 0xFFFFFFFF),
                   ("travel", 0x3627, TOKEN_VALUE, "yes"), ("check_start_date", 0x3614, BOOL, True),
                   ("travel_plan_draft_created", 0x37BC, TRAVEL_PLAN, None)],
    },
    "decline_activity_invite_command": {
        "token": 0x39CA, "vtable_rva": 0x477C520,
        "write_rva": 0x298A9D0, "read_rva": 0x298AA70,
        "fields": [("activity", 0x2816, U32, 0xFFFFFFFF), ("character", 0x6EC, U32, 0xFFFFFFFF)],
    },
    "start_activity_command": {
        "token": 0x362C, "vtable_rva": 0x477C070,
        "write_rva": 0x2989DC0, "read_rva": 0x2989E70,
        "encoding_scope": "Activity hosting command carrying native CActivityPlan data",
        "fields": [("data", 0xF0, ACTIVITY_PLAN, None)],
    },
    "set_activity_intent_command": {
        "token": 0x3803, "vtable_rva": 0x477C1D0,
        "write_rva": 0x298C100, "read_rva": 0x298C1A0,
        "fields": [("character", 0x6EC, U32, 0xFFFFFFFF), ("intent", 0x362D, ACTIVITY_INTENT, None)],
    },
    "raise_troops": {
        "token": 0x3216, "vtable_rva": 0x45448E0,
        "write_rva": 0x2999420, "read_rva": 0x2999590,
        "enums": {"raise_army_type": {"raid": 0x339E, "regular": 0x31BB,
                                      "men_at_arms": 0x2C87, "barter": 0x3E25, "none": 0x165}},
        "fields": [("character", 0x6EC, U32, None), ("rally_points", 0x3212, RALLY_POINTS, []),
                   ("rally_point", 0x3214, I32, -1), ("raise_army_type", 0x36DE, TOKEN_VALUE, None),
                   ("supply_limit", 0x2D6A, BOOL, False)],
    },
    "move_unit": {
        "token": 0x27FB, "vtable_rva": 0x477BDA8,
        "write_rva": 0x29766E0, "read_rva": 0x2976A30,
        "encoding_scope": "Destination movement using the game's pathfinder",
        "enums": {"valid_for": VALID_FOR, "unit_order_type": {"set": 0x340F, "append": 0x29E5, "invalid": 0x4FC},
                  "type": {"land": 0x2FB5, "water": 0x2FB6, "any": 0x2FC0},
                  "gathering_army": {"reset": 0x608, "default": 0x346}},
        "fields": [("valid_for", 0x340A, TOKEN_VALUE, None), ("unit", 0x27F7, U32, None),
                   ("target", 0x6B, I32, None), ("unit_order_type", 0x3410, TOKEN_VALUE, None),
                   ("type", 0xE1, TOKEN_VALUE, None), ("gathering_army", 0x29BD, TOKEN_VALUE, None)],
    },
    "disband_army": {
        "token": 0x2BAE, "vtable_rva": 0x477BB80,
        "write_rva": 0x2977280, "read_rva": 0x29772E0,
        "enums": {"valid_for": VALID_FOR},
        "fields": [("valid_for", 0x340A, TOKEN_VALUE, None), ("army", 0x296A, U32, None)],
    },
    "stop_gathering_army": {
        "token": 0x3472, "vtable_rva": 0x477BC48,
        "write_rva": 0x2977280, "read_rva": 0x29772E0,
        "enums": {"valid_for": VALID_FOR},
        "fields": [("valid_for", 0x340A, TOKEN_VALUE, None), ("army", 0x296A, U32, None)],
    },
    "send_character_interaction": {
        "token": 0x28B3, "vtable_rva": 0x449BE60,
        "write_rva": 0x2974FE0, "read_rva": 0x2975090,
        "encoding_scope": "War, marriage and supported ordinary interactions with character and boolean scopes",
        "fields": [("character_interaction", 0x28A9, INTERACTION_DATA, None),
                   ("ai_intermediary_accept", 0x3742, FIXED_POINT, None), ("ai_accept", 0x28A4, FIXED_POINT, None)],
    },
    "select_commander": {
        "token": 0x2D68, "vtable_rva": 0x477B310,
        "write_rva": 0x297E550, "read_rva": 0x297E5F0,
        "enums": {"valid_for": VALID_FOR},
        "fields": [("valid_for", 0x340A, TOKEN_VALUE, None), ("character", 0x6EC, U32, None),
                   ("army", 0x296A, U32, None)],
    },
    "detach_commander": {
        "token": 0x2D69, "vtable_rva": 0x477B3A8,
        "write_rva": 0x2977280, "read_rva": 0x29772E0,
        "enums": {"valid_for": VALID_FOR},
        "fields": [("valid_for", 0x340A, TOKEN_VALUE, None), ("army", 0x296A, U32, None)],
    },
    "hire_mercenary": {
        "token": 0x31C2, "vtable_rva": 0x477E608,
        "write_rva": 0x29A10D0, "read_rva": 0x29A1240,
        "fields": [("character", 0x6EC, U32, None), ("mercenary_company", 0x3147, U32, None),
                   ("flags", 0x6E4, I32, 3)],
    },
    "extend_mercenary": {
        "token": 0x31C5, "vtable_rva": 0x477E798,
        "write_rva": 0x29A1FA0, "read_rva": 0x29A20C0,
        "fields": [("character", 0x6EC, U32, None), ("mercenary_company", 0x3147, U32, None),
                   ("cost", 0x2B25, BOOL, False), ("war", 0x2B3F, BOOL, False)],
    },
    "hire_holy_order": {
        "token": 0x30AC, "vtable_rva": 0x477E6A0,
        "write_rva": 0x29A1600, "read_rva": 0x29A1720,
        "fields": [("character", 0x6EC, U32, None), ("holy_order", 0x30A3, U32, None),
                   ("cost", 0x2B25, BOOL, False), ("war", 0x2B3F, BOOL, False)],
    },
    "stop": {
        "token": 0x006A, "vtable_rva": 0x477BAE8,
        "write_rva": 0x2977160, "read_rva": 0x29771A0,
        "enums": {"valid_for": VALID_FOR},
        "fields": [("valid_for", 0x340A, TOKEN_VALUE, None), ("units", 0x27F8, U32_LIST, [])],
    },
    "merge_units": {
        "token": 0x2FA1, "vtable_rva": 0x477B890,
        "write_rva": 0x297BDA0, "read_rva": 0x297BE20,
        "enums": {"valid_for": VALID_FOR},
        "fields": [("valid_for", 0x340A, TOKEN_VALUE, None), ("target", 0x6B, U32, None),
                   ("units", 0x27F8, U32_LIST, [])],
    },
    "split_half_army": {
        "token": 0x2C0B, "vtable_rva": 0x477BCE0,
        "write_rva": 0x2979B70, "read_rva": 0x2979C10,
        "enums": {"valid_for": VALID_FOR},
        "fields": [("valid_for", 0x340A, TOKEN_VALUE, None), ("actor", 0x28AA, U32, None),
                   ("army", 0x296A, U32, None)],
    },
    "start_assault": {
        "token": 0x3163, "vtable_rva": 0x477AC38,
        "write_rva": 0x2980220, "read_rva": 0x29802C0,
        "enums": {"valid_for": VALID_FOR},
        "fields": [("valid_for", 0x340A, TOKEN_VALUE, None), ("siege", 0x2C3E, U32, None),
                   ("character", 0x6EC, U32, None)],
    },
    "stop_assault": {
        "token": 0x3164, "vtable_rva": 0x477ACD0,
        "write_rva": 0x2980220, "read_rva": 0x29802C0,
        "enums": {"valid_for": VALID_FOR},
        "fields": [("valid_for", 0x340A, TOKEN_VALUE, None), ("siege", 0x2C3E, U32, None),
                   ("character", 0x6EC, U32, None)],
    },
    "change_council_task": {
        "token": 0x2E4B, "vtable_rva": 0x477E8A8,
        "write_rva": 0x29A34E0, "read_rva": 0x29A3630,
        "fields": [("character", 0x6EC, U32, 0xFFFFFFFF), ("liege", 0x292D, U32, 0xFFFFFFFF),
                   ("active_council_task", 0x2E2B, U32, 0xFFFFFFFF),
                   ("task_type", 0x2E08, STRING, None), ("target", 0x6B, SCOPE_VALUE, {})],
    },
    "fire_council_position": {
        "token": 0x2DDC, "vtable_rva": 0x477E970,
        "write_rva": 0x29A2DA0, "read_rva": 0x29A2E70,
        "fields": [("liege", 0x292D, U32, None), ("character", 0x6EC, U32, None),
                   ("position", 0x4C, U32, None)],
    },
    "swap_council_position": {
        "token": 0x2DDB, "vtable_rva": 0x477EA08,
        "write_rva": 0x29A2A10, "read_rva": 0x29A2AE0,
        "fields": [("liege", 0x292D, U32, None), ("character", 0x6EC, U32, None),
                   ("target", 0x6B, U32, None)],
    },
    "leave_council_position": {
        "token": 0x3160, "vtable_rva": 0x477EAD0,
        "write_rva": 0x2895E20, "read_rva": 0x2895E80,
        "fields": [("character", 0x6EC, U32, None)],
    },
    "end_scheme": {
        "token": 0x2921, "vtable_rva": 0x477F970,
        "write_rva": 0x29A90F0, "read_rva": 0x29A9150,
        "fields": [("scheme", 0x290A, U32, None)],
    },
    "expose_secret": {
        "token": 0x325B, "vtable_rva": 0x47831C8,
        "write_rva": 0x29C3860, "read_rva": 0x29C38F0,
        "fields": [("secret", 0x28FF, U32, None), ("character", 0x6EC, U32, None)],
    },
    "set_countermeasure": {
        "token": 0x38D8, "vtable_rva": 0x4783320,
        "write_rva": 0x29C3BE0, "read_rva": 0x29C3D20,
        "fields": [("character", 0x6EC, U32, 0xFFFFFFFF),
                   ("scheme_countermeasure_type", 0x38D9, STRING, None)],
    },
    "toggle_scheme_agents_auto_assign": {
        "token": 0x3C64, "vtable_rva": 0x477F8A8,
        "write_rva": 0x29A95E0, "read_rva": 0x2895E80,
        "fields": [("character", 0x6EC, U32, 0xFFFFFFFF)],
    },
    "create_title": {
        "token": 0x2CC0, "vtable_rva": 0x44AC9F8,
        "write_rva": 0x2898E20, "read_rva": 0x2898EB0,
        "fields": [("character", 0x6EC, U32, None), ("landed_title", 0x27ED, U32, None)],
    },
    "destroy_title": {
        "token": 0x2CC1, "vtable_rva": 0x44ACAC0,
        "write_rva": 0x2899300, "read_rva": 0x28993D0,
        "fields": [("character", 0x6EC, U32, None), ("landed_title", 0x27ED, U32, None),
                   ("cost", 0x2B25, BOOL, None)],
    },
    "usurp_title": {
        "token": 0x2CCB, "vtable_rva": 0x44AC868,
        "write_rva": 0x2898E20, "read_rva": 0x2898EB0,
        "fields": [("character", 0x6EC, U32, None), ("landed_title", 0x27ED, U32, None)],
    },
    "set_primary_title": {
        "token": 0x2FC8, "vtable_rva": 0x44AC930,
        "write_rva": 0x2898E20, "read_rva": 0x2898EB0,
        "fields": [("character", 0x6EC, U32, None), ("landed_title", 0x27ED, U32, None)],
    },
    "set_realm_capital": {
        "token": 0x2F88, "vtable_rva": 0x44AC7A0,
        "write_rva": 0x2899EC0, "read_rva": 0x2899F90,
        "fields": [("cooldown", 0x299C, BOOL, None), ("character", 0x6EC, U32, None),
                   ("landed_title", 0x27ED, U32, None)],
    },
    "join_faction": {
        "token": 0x2F5E, "vtable_rva": 0x4783708,
        "write_rva": 0x29C4610, "read_rva": 0x29C46A0,
        "fields": [("faction", 0x2EE4, U32, None), ("character", 0x6EC, U32, None)],
    },
    "leave_faction": {
        "token": 0x2F5D, "vtable_rva": 0x47833E8,
        "write_rva": 0x29C4610, "read_rva": 0x29C46A0,
        "fields": [("faction", 0x2EE4, U32, None), ("character", 0x6EC, U32, None)],
    },
    "destroy_faction": {
        "token": 0x2F5F, "vtable_rva": 0x4783548,
        "write_rva": 0x29C4920, "read_rva": 0x29C4980,
        "fields": [("faction", 0x2EE4, U32, None)],
    },
    "faction_press_demand": {
        "token": 0x2F63, "vtable_rva": 0x4783610,
        "write_rva": 0x29C4920, "read_rva": 0x29C4980,
        "fields": [("faction", 0x2EE4, U32, None)],
    },
    "give_court_position": {
        "token": 0x3689, "vtable_rva": 0x477CAD8,
        "write_rva": 0x298DA70, "read_rva": 0x298DBF0,
        "fields": [("employer", 0x280A, U32, 0xFFFFFFFF), ("employee", 0x368D, U32, 0xFFFFFFFF),
                   ("court_position", 0x3688, STRING, None)],
    },
    "revoke_court_position": {
        "token": 0x368A, "vtable_rva": 0x477CA10,
        "write_rva": 0x298DD20, "read_rva": 0x298DD80,
        "fields": [("court_position", 0x3688, U32, 0xFFFFFFFF)],
    },
    "cancel_travel_plan_command": {
        "token": 0x37BD, "vtable_rva": 0x47821F0,
        "write_rva": 0x29BC4D0, "read_rva": 0x29BC530,
        "fields": [("travel_plan", 0x37B6, U32, 0xFFFFFFFF)],
    },
    "dismiss_important_action": {
        "token": 0x2E5C, "vtable_rva": 0x477FD28,
        "write_rva": 0x29AA9A0, "read_rva": 0x29AAA00,
        "fields": [("important_action_item", 0x2E34, U32, 0xFFFFFFFF)],
    },
    "accept_task_contract": {
        "token": 0x39AC, "vtable_rva": 0x4783F68,
        "write_rva": 0x29CA070, "read_rva": 0x29CA0D0,
        "fields": [("task_contract", 0x399E, U32, 0xFFFFFFFF)],
    },
    "add_law": {
        "token": 0x2FF3, "vtable_rva": 0x4771AB0,
        "write_rva": 0x289A9F0, "read_rva": 0x289AA90,
        "fields": [("character", 0x6EC, U32, None), ("law", 0x2FF2, STRING, None)],
    },
    "add_title_law": {
        "token": 0x3453, "vtable_rva": 0x4771468,
        "write_rva": 0x289D850, "read_rva": 0x289D920,
        "fields": [("title", 0x6CE, U32, None), ("character", 0x6EC, U32, None), ("law", 0x2FF2, I32, None)],
    },
    "clear_title_laws": {
        "token": 0x322D, "vtable_rva": 0x47713A0,
        "write_rva": 0x289CDE0, "read_rva": 0x289CE70,
        "fields": [("title", 0x6CE, U32, None), ("character", 0x6EC, U32, None)],
    },
    "nominate_successor_for_title": {
        "token": 0x3294, "vtable_rva": 0x45C5B38,
        "write_rva": 0x289C0F0, "read_rva": 0x289C200,
        "fields": [("elector", 0x3295, U32, None), ("title", 0x6CE, U32, None),
                   ("candidate", 0x2F42, U32, None), ("force", 0x19E, BOOL, False)],
    },
    "designate_diarch": {
        "token": 0x3727, "vtable_rva": 0x4783BD8,
        "write_rva": 0x29C94C0, "read_rva": 0x29C9550,
        "fields": [("liege", 0x292D, U32, None), ("designated_diarch", 0x3726, U32, None)],
    },
    "set_diarchy_mandate": {
        "token": 0x3730, "vtable_rva": 0x4783B10,
        "write_rva": 0x29C9750, "read_rva": 0x29C9890,
        "fields": [("liege", 0x292D, U32, None), ("mandate", 0x372F, STRING, None)],
    },
    "create_maa_regiment": {
        "token": 0x2D48, "vtable_rva": 0x477B470,
        "write_rva": 0x297C8C0, "read_rva": 0x297C9F0,
        "enums": {"valid_for": VALID_FOR, "cost": COST_CHOICE},
        "fields": [("valid_for", 0x340A, TOKEN_VALUE, None), ("title", 0x6CE, U32, 0xFFFFFFFF),
                   ("character", 0x6EC, U32, None), ("regiment", 0x2977, I32, None),
                   ("count", 0x45A, I32, None), ("cost", 0x2B25, TOKEN_VALUE, "yes")],
    },
    "add_maa_to_regiment": {
        "token": 0x2D4A, "vtable_rva": 0x45582E0,
        "write_rva": 0x297DC40, "read_rva": 0x297DCF0,
        "enums": {"valid_for": VALID_FOR, "cost": COST_CHOICE},
        "fields": [("valid_for", 0x340A, TOKEN_VALUE, None), ("regiment", 0x2977, U32, None),
                   ("count", 0x45A, I32, None), ("cost", 0x2B25, TOKEN_VALUE, "yes")],
    },
    "destroy_maa_regiment": {
        "token": 0x2D47, "vtable_rva": 0x477B120,
        "write_rva": 0x297D620, "read_rva": 0x297D680,
        "enums": {"valid_for": VALID_FOR},
        "fields": [("valid_for", 0x340A, TOKEN_VALUE, None), ("regiment", 0x2977, U32, None)],
    },
    "set_maa_reinforce": {
        "token": 0x2D4F, "vtable_rva": 0x477B058,
        "write_rva": 0x297DEF0, "read_rva": 0x297DF90,
        "enums": {"valid_for": VALID_FOR},
        "fields": [("valid_for", 0x340A, TOKEN_VALUE, None), ("character", 0x6EC, U32, None),
                   ("reinforce", 0x2D50, BOOL, None)],
    },
    "reorganize_army": {
        "token": 0x3104, "vtable_rva": 0x477B738,
        "write_rva": 0x297A160, "read_rva": 0x297A2A0,
        "enums": {"valid_for": VALID_FOR},
        "fields": [("valid_for", 0x340A, TOKEN_VALUE, None), ("army", 0x296A, U32, None),
                   ("merge_army", 0x2C0A, U32, 0xFFFFFFFF), ("select_unit", 0x2C0C, I32, None),
                   ("disband_army", 0x2BAE, I32, None), ("regiments", 0x2969, U32_LIST, [])],
    },
    "split_army_for_siege": {
        "token": 0x2F90, "vtable_rva": 0x477B958,
        "write_rva": 0x297B260, "read_rva": 0x297B340,
        "enums": {"valid_for": VALID_FOR},
        "fields": [("valid_for", 0x340A, TOKEN_VALUE, None), ("actor", 0x28AA, U32, None),
                   ("army", 0x296A, U32, None), ("soldiers_to_stay", 0x2F91, I32, None)],
    },
    "attach_to_unit": {
        "token": 0x35AC, "vtable_rva": 0x477A9E0,
        "write_rva": 0x2981EE0, "read_rva": 0x2981F90,
        "enums": {"valid_for": VALID_FOR},
        "fields": [("valid_for", 0x340A, TOKEN_VALUE, None), ("unit", 0x27F7, U32, 0xFFFFFFFF),
                   ("to", 0x529, U32, 0xFFFFFFFF)],
    },
    "detatch_from_unit": {
        "token": 0x35AD, "vtable_rva": 0x477AA78,
        "write_rva": 0x2982250, "read_rva": 0x29822C0,
        "enums": {"valid_for": VALID_FOR},
        "fields": [("valid_for", 0x340A, TOKEN_VALUE, None), ("unit", 0x27F7, U32, 0xFFFFFFFF)],
    },
    "toggle_raiding": {
        "token": 0x3446, "vtable_rva": 0x477AE30,
        "write_rva": 0x297E550, "read_rva": 0x297E5F0,
        "enums": {"valid_for": VALID_FOR},
        "fields": [("valid_for", 0x340A, TOKEN_VALUE, None), ("character", 0x6EC, U32, None),
                   ("army", 0x296A, U32, None)],
    },
    "raid": {
        "token": 0x339E, "vtable_rva": 0x477AD98,
        "write_rva": 0x297E550, "read_rva": 0x297E5F0,
        "enums": {"valid_for": VALID_FOR},
        "fields": [("valid_for", 0x340A, TOKEN_VALUE, None), ("character", 0x6EC, U32, None),
                   ("army", 0x296A, U32, None)],
    },
    "knight_status": {
        "token": 0x3106, "vtable_rva": 0x47716C0,
        "write_rva": 0x289CAB0, "read_rva": 0x289CB10,
        "enums": {"knight_status": KNIGHT_STATUS_VALUES},
        "fields": [("character", 0x6EC, U32, None), ("knight_status", 0x3106, TOKEN_VALUE, None)],
    },
    "add_rally_point": {
        "token": 0x3215, "vtable_rva": 0x477DB70,
        "write_rva": 0x2998920, "read_rva": 0x29989B0,
        "fields": [("character", 0x6EC, U32, None), ("target", 0x6B, I32, None)],
    },
    "move_rally_point": {
        "token": 0x3213, "vtable_rva": 0x477DAD8,
        "write_rva": 0x2998650, "read_rva": 0x2998720,
        "fields": [("character", 0x6EC, U32, None), ("rally_point", 0x3214, I32, None),
                   ("target", 0x6B, I32, None)],
    },
    "remove_rally_point": {
        "token": 0x3217, "vtable_rva": 0x477DA10,
        "write_rva": 0x2998920, "read_rva": 0x29989B0,
        "fields": [("character", 0x6EC, U32, None), ("target", 0x6B, I32, None)],
    },
    "mass_prisoner_action": {
        "token": 0x3444, "vtable_rva": 0x4779BA0,
        "write_rva": 0x2975910, "read_rva": 0x29759E0,
        "enums": {"interaction": MASS_PRISONER_INTERACTIONS},
        "fields": [("interaction", 0x2E4C, TOKEN_VALUE, None), ("actor", 0x28AA, U32, 0xFFFFFFFF),
                   ("prisoners", 0x3309, U32_LIST, [])],
    },
    "equip_artifact": {
        "token": 0x359C, "vtable_rva": 0x4781908,
        "write_rva": 0x29B6C30, "read_rva": 0x29B6D00,
        "fields": [("character", 0x6EC, U32, None), ("artifact", 0x3594, U32, None),
                   ("replace", 0x632, U32, None)],
    },
    "unequip_artifact": {
        "token": 0x359D, "vtable_rva": 0x47817A8,
        "write_rva": 0x29B6EC0, "read_rva": 0x29B6F50,
        "fields": [("character", 0x6EC, U32, None), ("artifact", 0x3594, U32, None)],
    },
    "start_court_position_task": {
        "token": 0x3998, "vtable_rva": 0x477C948,
        "write_rva": 0x298E5C0, "read_rva": 0x298E700,
        "fields": [("court_position", 0x3688, U32, 0xFFFFFFFF), ("task_type", 0x2E08, STRING, None)],
    },
    "stop_court_position_task": {
        "token": 0x3999, "vtable_rva": 0x477C8B0,
        "write_rva": 0x298DD20, "read_rva": 0x298DD80,
        "fields": [("court_position", 0x3688, U32, 0xFFFFFFFF)],
    },
    "select_culture_fascination": {
        "token": 0x3138, "vtable_rva": 0x477E4C8,
        "write_rva": 0x299B730, "read_rva": 0x299B810,
        "fields": [("player", 0x274A, U32, 0xFFFFFFFF), ("culture", 0x27F4, U32, 0xFFFFFFFF),
                   ("culture_innovation", 0x2F81, I32, None)],
    },
    "add_dynasty_perk": {
        "token": 0x335B, "vtable_rva": 0x4770E28,
        "write_rva": 0x2898800, "read_rva": 0x2898A20,
        "optional_fields": ["trait"],
        "fields": [("target", 0x6B, U32, None), ("perk", 0x2965, STRING, None),
                   ("trait", 0x280F, STRING, None)],
    },
    "create_faction": {
        "token": 0x2F60, "vtable_rva": 0x4783480,
        "write_rva": 0x29C4C50, "read_rva": 0x29C4D20,
        "fields": [("faction_type", 0x2F61, I32, None), ("character", 0x6EC, U32, None),
                   ("target", 0x6B, U32, None)],
    },
    "add_construction": {
        "token": 0x2D89, "vtable_rva": 0x477CD48,
        "write_rva": 0x298F210, "read_rva": 0x298F320,
        "fields": [("character", 0x6EC, U32, None), ("province", 0x27D2, I32, None),
                   ("index", 0x20C, I32, None), ("type", 0xE1, I32, None)],
    },
    "cancel_construction": {
        "token": 0x2D8B, "vtable_rva": 0x477CF08,
        "write_rva": 0x298F680, "read_rva": 0x298F710,
        "fields": [("character", 0x6EC, U32, None), ("province", 0x27D2, I32, None)],
    },
    "pause_game_command": {
        "token": 0x2E59, "vtable_rva": 0x477D440,
        "write_rva": 0x29946C0, "read_rva": 0x2994750,
        "fields": [("player", 0x274A, I32, None), ("value", 0x2D2, BOOL, None)],
    },
    "close_succession": {
        "token": 0x3C00, "vtable_rva": 0x4770590,
        "write_rva": 0x28A1CF0, "read_rva": 0x28A1D90,
        "fields": [("character", 0x6EC, U32, 0xFFFFFFFF), ("machineid", 0x16, I32, None)],
    },
    "set_speed": {
        "token": 0x29ED, "vtable_rva": 0x477D348,
        "write_rva": 0x29943A0, "read_rva": 0x2994400,
        "fields": [("speed", 0x6E, I32, None)],
    },
    "client_ping": {
        "token": 0x2788, "vtable_rva": 0x477D280,
        "write_rva": 0x29945D0, "read_rva": 0x2994660,
        "fields": [("turn", 0x278A, U32, None), ("machineid", 0x16, U32, None)],
    },
    "set_ready_command": {
        "token": 0x6A6, "vtable_rva": 0x492F870,
        "write_rva": 0x368D4B0, "read_rva": 0x368D540,
        "fields": [("machineid", 0x16, I32, None), ("status", 0xD0, BOOL, None)],
    },
    "set_character_controller": {
        "token": 0x6DE, "vtable_rva": 0x492F2E0,
        "write_rva": 0x368B3C0, "read_rva": 0x368B490,
        "fields": [
            ("machineid", 0x16, I32, None), ("playable", 0x6DF, U32, None),
            ("leave_ai_on", 0x5C2, BOOL, False),
        ],
    },
    "filetransfer_fast_cmd_request_handshake": {
        "token": 0x618, "vtable_rva": 0x4954FF8,
        "write_rva": 0x3832F40, "read_rva": 0x3834D60,
        "fields": TRANSFER_FIELDS,
    },
    "filetransfer_fast_cmd_handshake": {
        "token": 0x619, "vtable_rva": 0x4955090,
        "write_rva": 0x3835420, "read_rva": 0x3835480,
        "fields": TRANSFER_FIELDS + [("filetransfer_fast_fld_chunk_size", 0x61D, U32, None)],
    },
    "filetransfer_fast_cmd_initialize": {
        "token": 0x61A, "vtable_rva": 0x4955128,
        "write_rva": 0x3832F40, "read_rva": 0x3832F50,
        "fields": TRANSFER_FIELDS,
    },
    "filetransfer_fast_cmd_chunk": {
        "token": 0x61B, "vtable_rva": 0x49551C0,
        "write_rva": 0x38341C0, "read_rva": 0x3834230,
        "fields": TRANSFER_FIELDS + [("chunk_id", 0x6CA, I32, None), ("data", 0xF0, BLOB, None)],
    },
    "filetransfer_fast_cmd_chunkstatus": {
        "token": 0x61C, "vtable_rva": 0x4955258,
        "write_rva": 0x3836410, "read_rva": 0x3836450,
        "fields": TRANSFER_FIELDS + [("data", 0xF0, BLOB, None)],
    },
}
HEADER_FIELDS = [
    ("identity", 0xDB, U32), ("sender", 0x240, I32),
    ("receiver", 0xC6, I32), ("attribs", 0x69C, U32),
]


def _word(value):
    return struct.pack("<H", value)


def _scalar(tag, value):
    if tag == BOOLEAN_PARAMETERS:
        return _decision_parameters(value)
    if tag == SCOPE_VALUE:
        return _scope_value(value)
    if tag == INTERACTION_DATA:
        return _character_interaction(value)
    if tag == ACTIVITY_INTENT:
        return _activity_intent(value)
    if tag == TRAVEL_PLAN:
        return _travel_plan(value)
    if tag == ACTIVITY_PLAN:
        return _activity_plan(value)
    if tag == TOKEN_VALUE:
        return _word(value)
    if tag == RALLY_POINTS:
        if not isinstance(value, list) or len(value) > MAX_MESSAGE_SIZE // 24:
            raise ValueError("rally_points must be a bounded array")
        records = bytearray()
        for item in value:
            if not isinstance(item, dict) or set(item) != {"province", "color"}:
                raise ValueError("Each rally point requires province and color")
            records.extend(_word(OPEN) + _property(0x27D2, _scalar(I32, item["province"]))
                           + _property(0x56, _scalar(I32, item["color"])) + _word(CLOSE))
        return _word(OPEN) + records + _word(CLOSE)
    if tag in LIST_TYPES:
        if not isinstance(value, list):
            raise ValueError("Command array fields require JSON arrays")
        if len(value) > MAX_MESSAGE_SIZE // 4:
            raise ValueError("Command array exceeds the PDX frame buffer size")
        if tag == U8_LIST and any(type(item) is not int or not 0 <= item <= 255 for item in value):
            raise ValueError("Player flags must fit their native byte-sized storage")
        encoded = bytearray(_word(OPEN))
        for item in value:
            encoded.extend(_scalar(LIST_TYPES[tag], item))
            if len(encoded) > MAX_MESSAGE_SIZE - 2:
                raise ValueError("Command array exceeds the PDX frame buffer size")
        return bytes(encoded) + _word(CLOSE)
    if tag == STRING:
        if not isinstance(value, str):
            raise ValueError("String command fields require strings")
        data = value.encode("utf-8")
        if len(data) > 0xFFFF:
            raise ValueError("String command field exceeds its uint16 byte length")
        return _word(tag) + _word(len(data)) + data
    if tag == BLOB:
        if not isinstance(value, dict) or set(value) != {"hex"}:
            raise ValueError("Binary command fields require an object with a hex string")
        data = bytes.fromhex(value["hex"])
        if len(data) > MAX_MESSAGE_SIZE:
            raise ValueError("Binary command field exceeds the PDX frame buffer size")
        return _word(tag) + struct.pack("<I", len(data)) + data
    if tag == BOOL:
        if type(value) is not bool:
            raise ValueError("Boolean command fields require JSON true or false")
        return _word(tag) + bytes([value])
    if tag == FIXED_POINT:
        if type(value) not in (int, float) or (type(value) is float and not math.isfinite(value)):
            raise ValueError("Fixed-point command fields require finite numbers")
        if abs(value) > (1 << 63) / 100000:
            raise ValueError("Fixed-point command field is outside the int64 range")
        integer = round(value * 100000)
        if not -(1 << 63) <= integer < 1 << 63:
            raise ValueError("Fixed-point command field is outside the int64 range")
        return _word(tag) + struct.pack("<q", integer)
    if type(value) is not int:
        raise ValueError("Integer command fields require integer values")
    if tag == I32:
        if not -(1 << 31) <= value < (1 << 31):
            raise ValueError("Signed command field is outside the int32 range")
        return _word(tag) + struct.pack("<i", value)
    if tag == U64:
        if not 0 <= value < 1 << 64:
            raise ValueError("Unsigned command field is outside the uint64 range")
        return _word(tag) + struct.pack("<Q", value)
    if not 0 <= value < (1 << 32):
        raise ValueError("Unsigned command field is outside the uint32 range")
    return _word(tag) + struct.pack("<I", value)


def _property(token, value):
    return _word(token) + _word(EQUAL) + value


def _decision_parameters(parameters):
    if not isinstance(parameters, dict) or set(parameters) - {"event_targets"}:
        raise ValueError("Decision parameters currently support boolean event_targets")
    targets = parameters.get("event_targets", {})
    if not isinstance(targets, dict):
        raise ValueError("Decision event_targets require a named JSON object")
    items = bytearray()
    for name, scope in targets.items():
        if (not isinstance(scope, dict) or set(scope) - {"type", "identity"}
                or scope.get("type") != "boolean"):
            raise ValueError("Decision event targets currently require boolean scope values")
        identity = scope.get("identity", 0)
        if type(identity) is not int or identity not in (0, 1):
            raise ValueError("Boolean scope identity must be integer 0 or 1")
        value = _property(0xE1, _word(0x500))
        if identity:
            value += _property(0xDB, _scalar(U64, identity))
        items.extend(_scalar(STRING, name) + _word(EQUAL) + _word(OPEN) + value + _word(CLOSE))
        if len(items) > MAX_MESSAGE_SIZE:
            raise ValueError("Decision parameters exceed the PDX frame buffer size")
    body = _property(0x6F8, _word(OPEN) + items + _word(CLOSE)) if items else b""
    return _word(OPEN) + body + _word(CLOSE)


def _scope_value(scope):
    if not isinstance(scope, dict) or set(scope) - {"type", "identity"}:
        raise ValueError("Interaction scopes require type and optional identity")
    if not scope:
        return _word(OPEN) + _word(CLOSE)
    tokens = {
        "char": 0x2EA6, "boolean": 0x500,
        "prov": 0x04FF, "province": 0x04FF,
        "lt": 0x2EA7, "landed_title": 0x2EA7,
        "artifact": 0x3594,
        "war": 0x2B3F,
        "court_position": 0x3688, "court_position_type": 0x3C0C,
        "council_task": 0x315F,
    }
    kind = scope.get("type")
    if not isinstance(kind, str) or kind not in tokens:
        raise ValueError("Unsupported interaction scope type: " + str(kind))
    identity = scope.get("identity", 0)
    maximum = 1 if kind == "boolean" else 0xffffffff
    if type(identity) is not int or not 0 <= identity <= maximum:
        raise ValueError("Interaction scope identity is outside its native range")
    body = _property(0xE1, _word(tokens[kind]))
    if identity:
        body += _property(0xDB, _scalar(U64, identity))
    return _word(OPEN) + body + _word(CLOSE)


def _interaction_scope(scope):
    if not isinstance(scope, dict) or set(scope) - {"root", "seed", "event_targets"}:
        raise ValueError("Interaction scope supports root, seed and event_targets")
    body = _property(0x6AC, _scope_value(scope.get("root", {})))
    body += _property(0x6F7, _scalar(I32, scope.get("seed", -1)))
    targets = scope.get("event_targets", {})
    if not isinstance(targets, dict):
        raise ValueError("Interaction event_targets require a named object")
    encoded = bytearray()
    for key, value in targets.items():
        if not isinstance(key, str) or not key:
            raise ValueError("Interaction target names must be nonempty strings")
        encoded.extend(_scalar(STRING, key) + _word(EQUAL) + _scope_value(value))
        if len(encoded) > MAX_MESSAGE_SIZE:
            raise ValueError("Interaction scopes exceed the PDX frame buffer")
    if encoded:
        body += _property(0x6F8, _word(OPEN) + encoded + _word(CLOSE))
    return _word(OPEN) + body + _word(CLOSE)


def _war_declaration(war):
    allowed = {"casus_belli", "titles", "claimant", "uses_hook", "has_hook"}
    if not isinstance(war, dict) or set(war) - allowed or not war.get("casus_belli"):
        raise ValueError("War declaration requires casus_belli and supported war fields")
    body = _property(0x289A, _scalar(STRING, war["casus_belli"]))
    titles = war.get("titles", [])
    encoded_titles = _scalar(U32_LIST, titles)
    if titles:
        body += _property(0x27D5, encoded_titles)
    body += _property(0x2DAF, _scalar(U32, war.get("claimant", 0xffffffff)))
    body += _property(0x3CF6, _scalar(BOOL, war.get("uses_hook", False)))
    body += _property(0x3CF7, _scalar(BOOL, war.get("has_hook", False)))
    return _word(OPEN) + body + _word(CLOSE)


def _culture_traits_block(traits):
    if not isinstance(traits, dict):
        raise ValueError("Culture traits payload requires a JSON object")
    allowed = {"traditions", *(key for key, _ in CULTURE_TRAIT_PILLAR_TOKENS)}
    if set(traits) - allowed:
        raise ValueError("Unsupported culture traits fields")
    body = bytearray()
    traditions = traits.get("traditions", [])
    if traditions:
        body.extend(_property(0x352E, _scalar(STRING_LIST, traditions)))
    for key, token in CULTURE_TRAIT_PILLAR_TOKENS:
        value = traits.get(key)
        if value is not None:
            body.extend(_property(token, _scalar(STRING, value)))
    return _word(OPEN) + bytes(body) + _word(CLOSE)


def _special_interaction_block(special_name, payload):
    spec = SPECIAL_INTERACTION_SPECS[special_name]
    if not isinstance(payload, dict):
        raise ValueError(f"Special interaction payload for {special_name} must be a JSON object")
    field_map = {field: (token, tag, default) for field, token, tag, default in spec["fields"]}
    if set(payload) - set(field_map):
        raise ValueError(f"Unsupported fields in {special_name} special payload")
    body = bytearray()
    for field, token, tag, default in spec["fields"]:
        if field not in payload:
            if default is None and spec["token"] in (0x2E72, 0x3735, 0x3C66):
                if field in ("title", "target", "domain", "recipient"):
                    raise ValueError(f"{special_name} requires {field}")
            if default is None:
                continue
            value = default
        else:
            value = payload[field]
        if tag == "culture_traits":
            body.extend(_property(token, _culture_traits_block(value)))
            continue
        if value is None:
            continue
        encoded = _scalar(tag, value)
        if default is None or value != default:
            body.extend(_property(token, encoded))
    return _word(OPEN) + bytes(body) + _word(CLOSE)


def _character_interaction(data):
    references = {"secondary_actor": (0x2BDC, 0xffffffff), "secondary_recipient": (0x2BDD, 0xffffffff),
                  "intermediary": (0x373F, 0xffffffff), "puppet_or_actor": (0x3F07, 0xffffffff)}
    if not isinstance(data, dict):
        raise ValueError("Character interaction data requires an object")
    name = data.get("character_interaction")
    specs = supported_interaction_specs()
    if name not in ("declare_war_interaction", *specs):
        raise ValueError("This character interaction is not supported by the encoder")
    special_name = "declare_war_interaction" if name == "declare_war_interaction" else specs[name]["special_interaction"]
    payload_key = special_name if special_name in ("declare_war_interaction", *SPECIAL_INTERACTION_SPECS) else None
    allowed = {"character_interaction", "scope", "original_scope", "actor", "recipient", "options", "target", *references}
    if payload_key is not None:
        allowed.add(payload_key)
    if not isinstance(data, dict) or set(data) - allowed:
        raise ValueError("Unsupported character interaction fields")
    required = {"scope", "original_scope", "actor", "recipient"}
    if payload_key is not None:
        required.add(payload_key)
    if not required <= set(data):
        raise ValueError("Character interaction requires scopes, actor, recipient and its native payload")
    body = _property(0x28A9, _scalar(STRING, data["character_interaction"]))
    body += _property(0x523, _interaction_scope(data["scope"]))
    body += _property(0x2FE1, _interaction_scope(data["original_scope"]))
    body += _property(0x28AA, _scalar(U32, data["actor"]))
    body += _property(0x28AB, _scalar(U32, data["recipient"]))
    for key, (token, default) in references.items():
        value = data.get(key, default)
        encoded = _scalar(U32, value)
        if value != default:
            body += _property(token, encoded)
    options = data.get("options", [])
    if not isinstance(options, list) or any(type(value) is not bool for value in options):
        raise ValueError("Interaction options require a boolean array")
    if len(options) > MAX_MESSAGE_SIZE // 16:
        raise ValueError("Interaction options exceed the PDX frame buffer")
    if options:
        encoded = _scalar(I32, len(options))
        for index, value in enumerate(options):
            if value:
                encoded += _scalar(I32, index) + _word(EQUAL) + _scalar(BOOL, value)
        body += _property(0x3641, _word(OPEN) + encoded + _word(CLOSE))
    target = data.get("target", {})
    encoded_target = _scope_value(target)
    if target:
        body += _property(0x6B, encoded_target)
    if name == "declare_war_interaction":
        body += _property(0x2CEA, _war_declaration(data["declare_war_interaction"]))
    elif payload_key is not None:
        spec = SPECIAL_INTERACTION_SPECS[payload_key]
        body += _property(spec["token"], _special_interaction_block(payload_key, data[payload_key]))
    return _word(OPEN) + body + _word(CLOSE)


def _unwrap_value_list(value, expected_type, label):
    if value == {}:
        return []
    if isinstance(value, list) and all(isinstance(item, dict) and set(item) == {"value"} for item in value):
        return [item["value"] for item in value]
    raise ValueError(f"Expected anonymous {label}")


def _normalize_character_interaction(data):
    if not isinstance(data, dict):
        return
    war = data.get("declare_war_interaction")
    if isinstance(war, dict) and "titles" in war:
        war["titles"] = _unwrap_value_list(war["titles"], int, "war target titles")
    for sp_name, spec in SPECIAL_INTERACTION_SPECS.items():
        block = data.get(sp_name)
        if not isinstance(block, dict):
            continue
        for field, _, tag, _ in spec["fields"]:
            if tag in LIST_TYPES and field in block:
                block[field] = _unwrap_value_list(block[field], object, f"{sp_name}.{field}")
            elif tag == "culture_traits" and isinstance(block.get(field), dict):
                traits = block[field]
                if "traditions" in traits:
                    traits["traditions"] = _unwrap_value_list(traits["traditions"], str, "culture traditions")
    if "options" in data:
        values = data["options"]
        if not isinstance(values, list) or not values or set(values[0]) != {"value"}:
            raise ValueError("Invalid interaction option count")
        count = values[0]["value"]
        if type(count) is not int or not 0 <= count <= MAX_MESSAGE_SIZE // 16:
            raise ValueError("Invalid interaction option count")
        options = [False] * count
        for item in values[1:]:
            key, value = item.get("key"), item.get("value")
            if type(key) is not int or not 0 <= key < count or type(value) is not bool:
                raise ValueError("Invalid sparse interaction option")
            options[key] = value
        data["options"] = options


def declare_war_fields(actor, target, casus_belli, *, titles=None, claimant=0xffffffff,
                       uses_hook=False, has_hook=False):
    """Build the ordinary actor/recipient context retained in native war requests."""
    if type(uses_hook) is not bool or type(has_hook) is not bool:
        raise ValueError("War hook flags require JSON true or false")
    characters = {"actor": actor, "recipient": target, "secondary_actor": 0xffffffff,
                  "secondary_recipient": 0xffffffff, "intermediary": 0xffffffff, "puppet_or_actor": actor}
    targets = {key: {"type": "char", "identity": value} for key, value in characters.items()}
    targets.update(is_puppet_action={"type": "boolean"},
                   hook={"type": "boolean", **({"identity": 1} if uses_hook else {})},
                   target={"type": "boolean", "identity": 1})
    return {
        "character_interaction": {
            "character_interaction": "declare_war_interaction", "actor": actor, "recipient": target,
            "puppet_or_actor": actor, "scope": {"root": {}, "seed": -1, "event_targets": targets},
            "original_scope": {"root": {}, "seed": -1}, "options": [uses_hook],
            "target": {"type": "boolean", "identity": 1},
            "declare_war_interaction": {"casus_belli": casus_belli, "titles": [] if titles is None else titles,
                                         "claimant": claimant, "uses_hook": uses_hook, "has_hook": has_hook},
        }, "ai_intermediary_accept": 100.0, "ai_accept": 1.0,
    }


def simple_interaction_fields(actor, recipient, interaction, *, ai_accept, flags=None,
                              secondary_actor=0xffffffff, secondary_recipient=0xffffffff,
                              target=None, ai_intermediary_accept=100.0, special_fields=None):
    """Build character/boolean/target contexts with explicitly supplied native acceptance values."""
    specs = supported_interaction_specs()
    if interaction not in specs:
        raise ValueError("Unsupported character interaction")
    meta = specs[interaction]
    if interaction == "arrange_marriage_interaction" and (secondary_actor == 0xffffffff or secondary_recipient == 0xffffffff):
        raise ValueError("Marriage requires both secondary_actor and secondary_recipient")
    expected_target = {"title": "landed_title", "artifact": "artifact"}.get(meta["target_type"])
    if expected_target is not None:
        if not isinstance(target, dict) or target.get("type") != expected_target or type(target.get("identity")) is not int:
            raise ValueError(f"{interaction} requires target {{'type': '{expected_target}', 'identity': <id>}}")
    elif meta["target_type"] is None and target is not None:
        if not isinstance(target, dict) or "type" not in target:
            raise ValueError("Interaction target requires {type, identity}")
    flags = {} if flags is None else flags
    if not isinstance(flags, dict) or any(not isinstance(key, str) or not key or type(value) is not bool
                                           for key, value in flags.items()):
        raise ValueError("Interaction flags require named JSON booleans")
    characters = {"actor": actor, "recipient": recipient, "secondary_actor": secondary_actor,
                  "secondary_recipient": secondary_recipient, "intermediary": 0xffffffff, "puppet_or_actor": actor}
    if set(flags) & (set(characters) | {"is_puppet_action", "target"}):
        raise ValueError("Interaction flags conflict with character scopes")
    special = meta["special_interaction"]
    end_war_target = (
        special in ("end_war_attacker_victory_interaction", "end_war_white_peace_interaction",
                    "end_war_attacker_defeat_interaction")
        and isinstance(target, dict) and target.get("type") == "war"
    )
    targets = {key: {"type": "char", "identity": value} for key, value in characters.items()}
    targets["is_puppet_action"] = {"type": "boolean"}
    if end_war_target:
        targets["war"] = target
    elif target is not None:
        targets["target"] = target
    targets.update((key, {"type": "boolean", **({"identity": 1} if value else {})}) for key, value in flags.items())
    data = {"character_interaction": interaction, **characters,
            "scope": {"root": target if end_war_target else {}, "seed": -1, "event_targets": targets},
            "original_scope": {"root": {}, "seed": -1}, "options": list(flags.values())}
    if target is not None and not end_war_target:
        data["target"] = target
    if special in SPECIAL_INTERACTION_SPECS:
        payload = special_fields
        if payload is None:
            if special in ("revoke_title_interaction", "diarch_revoke_title") and target is not None:
                payload = {"title": target["identity"]}
            elif special in ("grant_titles_interaction", "grant_governorship_interaction") and target is not None:
                payload = {"titles": [target["identity"]]}
            else:
                payload = {}
        data[special] = payload
    elif special_fields:
        raise ValueError(f"{interaction} does not accept special_fields")
    return {"character_interaction": data, "ai_accept": ai_accept, "ai_intermediary_accept": ai_intermediary_accept}


def _activity_intent(intent):
    if not isinstance(intent, dict) or set(intent) - {"type", "target"}:
        raise ValueError("Activity intent supports type and optional target")
    body = bytearray()
    kind = intent.get("type")
    if kind is not None:
        if not isinstance(kind, str) or not kind:
            raise ValueError("Activity intent type must be a nonempty string")
        body.extend(_property(0xE1, _scalar(STRING, kind)))
    target = intent.get("target", 0xFFFFFFFF)
    encoded_target = _scalar(U32, target)
    if target != 0xFFFFFFFF:
        body.extend(_property(0x6B, encoded_target))
    return _word(OPEN) + bytes(body) + _word(CLOSE)


def _bucket_array(buckets, tag, name):
    if not isinstance(buckets, list) or len(buckets) > MAX_MESSAGE_SIZE // 8:
        raise ValueError(f"{name} must be a bounded array")
    body = bytearray(_property(0x2F, _scalar(I32, len(buckets))))
    for index, group in enumerate(buckets):
        if not isinstance(group, list):
            raise ValueError(f"Each {name} bucket must be a JSON array")
        if not group:
            continue
        items = bytearray()
        for value in group:
            items.extend(_scalar(tag, value))
            if len(items) > MAX_MESSAGE_SIZE - 16:
                raise ValueError(f"{name} exceeds the PDX frame buffer")
        body.extend(_scalar(I32, index) + _word(EQUAL) + _word(OPEN) + items + _word(CLOSE))
        if len(body) > MAX_MESSAGE_SIZE - 16:
            raise ValueError(f"{name} exceeds the PDX frame buffer")
    return _word(OPEN) + bytes(body) + _word(CLOSE)


def _travel_trigger(trigger, name):
    if not isinstance(trigger, dict) or set(trigger) - {"event", "on_action"}:
        raise ValueError(f"Travel {name} supports optional event and on_action")
    body = bytearray()
    for field, token in (("event", 0x1B8), ("on_action", 0x281A)):
        value = trigger.get(field)
        if value is None or value == "":
            continue
        body.extend(_property(token, _scalar(STRING, value)))
    return _word(OPEN) + bytes(body) + _word(CLOSE)


def _travel_plan(plan):
    allowed = {
        "owner", "travel_leader", "companions", "activity", "task_contract",
        "options", "departure_date", "departure_location", "destinations",
        "path_duration", "max_path_duration", "travel_with_domicile",
        "migration", "can_cancel_planning", "waypoints", "modifier",
        "on_arrival", "on_start", "on_travel_planner_cancel",
        "on_arrival_destinations", "scope",
    }
    if not isinstance(plan, dict) or set(plan) - allowed:
        raise ValueError("Unsupported travel plan fields")
    if "departure_date" not in plan or "departure_location" not in plan:
        raise ValueError("Travel plan requires departure_date and departure_location")
    body = bytearray()
    for field, token in (("owner", 0x2812), ("travel_leader", 0x3787)):
        value = plan.get(field, 0xFFFFFFFF)
        encoded = _scalar(U32, value)
        if value != 0xFFFFFFFF:
            body.extend(_property(token, encoded))
    companions = plan.get("companions", [])
    if not isinstance(companions, list) or len(companions) > MAX_MESSAGE_SIZE // 32:
        raise ValueError("Travel companions must be a bounded array")
    if companions:
        records = bytearray()
        for item in companions:
            if (not isinstance(item, dict)
                    or set(item) - {"character", "date", "location", "source", "travel_option"}
                    or not {"character", "date", "location"} <= set(item)):
                raise ValueError("Travel companion requires character, date and location")
            entry = bytearray()
            entry.extend(_property(0x6EC, _scalar(U32, item["character"])))
            entry.extend(_property(0x6B5, _scalar(I32, item["date"])))
            entry.extend(_property(0x27F6, _scalar(I32, item["location"])))
            source = item.get("source")
            if source is not None and source != "none":
                if not isinstance(source, str) or source not in TRAVEL_COMPANION_SOURCES:
                    raise ValueError("Unsupported travel companion source")
                entry.extend(_property(0x31BD, _word(TRAVEL_COMPANION_SOURCES[source])))
            if "travel_option" in item and item["travel_option"] is not None:
                if not isinstance(item["travel_option"], str) or not item["travel_option"]:
                    raise ValueError("Companion travel_option must be a nonempty string")
                entry.extend(_property(0x37D2, _scalar(STRING, item["travel_option"])))
            records.extend(_word(OPEN) + bytes(entry) + _word(CLOSE))
        body.extend(_property(0x3789, _word(OPEN) + bytes(records) + _word(CLOSE)))
    for field, token in (("activity", 0x2816), ("task_contract", 0x399E)):
        value = plan.get(field, 0xFFFFFFFF)
        encoded = _scalar(U32, value)
        if value != 0xFFFFFFFF:
            body.extend(_property(token, encoded))
    options = plan.get("options", [])
    encoded_options = _scalar(STRING_LIST, options)
    if options:
        body.extend(_property(0x3641, encoded_options))
    body.extend(_property(0x37C0, _scalar(I32, plan["departure_date"])))
    body.extend(_property(0x37BF, _scalar(I32, plan["departure_location"])))
    destinations = plan.get("destinations", [])
    encoded_destinations = _scalar(I32_LIST, destinations)
    if destinations:
        body.extend(_property(0x37AC, encoded_destinations))
    for field, token in (("path_duration", 0x3981), ("max_path_duration", 0x3982)):
        value = plan.get(field, -1)
        encoded = _scalar(I32, value)
        if value != -1:
            body.extend(_property(token, encoded))
    for field, token, default in (
        ("travel_with_domicile", 0x3B8F, False),
        ("migration", 0x3C66, False),
        ("can_cancel_planning", 0x3B49, True),
    ):
        value = plan.get(field, default)
        encoded = _scalar(BOOL, value)
        if value != default:
            body.extend(_property(token, encoded))
    body.extend(_property(0x37C7, _bucket_array(plan.get("waypoints", []), I32, "waypoints")))
    modifiers = plan.get("modifier", [])
    if not isinstance(modifiers, list) or len(modifiers) > MAX_MESSAGE_SIZE // 24:
        raise ValueError("Travel modifier must be a bounded array")
    for item in modifiers:
        if (not isinstance(item, dict)
                or set(item) - {"modifier", "expiration_date", "multiplier"}
                or not {"modifier", "expiration_date"} <= set(item)):
            raise ValueError("Travel modifier requires modifier and expiration_date")
        if not isinstance(item["modifier"], str) or not item["modifier"]:
            raise ValueError("Travel modifier name must be a nonempty string")
        entry = bytearray()
        entry.extend(_property(0x27A9, _scalar(STRING, item["modifier"])))
        entry.extend(_property(0x2827, _scalar(I32, item["expiration_date"])))
        multiplier = item.get("multiplier", 1.0)
        encoded_multiplier = _scalar(FIXED_POINT, multiplier)
        if round(multiplier * 100000) != 100000:
            entry.extend(_property(0xCD4, encoded_multiplier))
        body.extend(_property(0x27A9, _word(OPEN) + bytes(entry) + _word(CLOSE)))
    for field, token in (
        ("on_arrival", 0x37A7), ("on_start", 0x4BC), ("on_travel_planner_cancel", 0x3A14),
    ):
        body.extend(_property(token, _travel_trigger(plan.get(field, {}), field)))
    arrival_mode = plan.get("on_arrival_destinations", "all_but_last")
    if not isinstance(arrival_mode, str) or arrival_mode not in TRAVEL_ON_ARRIVAL_DESTINATIONS:
        raise ValueError("Invalid on_arrival_destinations mode")
    if arrival_mode != "all_but_last":
        body.extend(_property(0x3A0B, _word(TRAVEL_ON_ARRIVAL_DESTINATIONS[arrival_mode])))
    body.extend(_property(0x523, _interaction_scope(plan.get("scope", {}))))
    return _word(OPEN) + bytes(body) + _word(CLOSE)


def _activity_plan(plan):
    allowed = {
        "type", "host", "intent", "start_date", "guest_delay_days",
        "invite_duration", "options", "phases", "special_guests",
        "invitations", "travel_plan", "variables", "tenets", "doctrines",
    }
    if not isinstance(plan, dict) or set(plan) - allowed:
        raise ValueError("Unsupported activity plan fields")
    if "start_date" not in plan:
        raise ValueError("Activity plan requires start_date")
    body = bytearray()
    kind = plan.get("type")
    if kind is not None:
        if not isinstance(kind, str) or not kind:
            raise ValueError("Activity plan type must be a nonempty string")
        body.extend(_property(0xE1, _scalar(STRING, kind)))
    host = plan.get("host", 0xFFFFFFFF)
    encoded_host = _scalar(U32, host)
    if host != 0xFFFFFFFF:
        body.extend(_property(0x2C27, encoded_host))
    body.extend(_property(0x362D, _activity_intent(plan.get("intent", {}))))
    body.extend(_property(0xCD5, _scalar(I32, plan["start_date"])))
    body.extend(_property(0x397D, _scalar(I32, plan.get("guest_delay_days", 0))))
    body.extend(_property(0x397E, _scalar(I32, plan.get("invite_duration", 0))))
    options = plan.get("options", {})
    if not isinstance(options, dict):
        raise ValueError("Activity options require a category-to-option JSON object")
    if options:
        entries = bytearray()
        for category, choice in options.items():
            if not isinstance(category, str) or not category or not isinstance(choice, str) or not choice:
                raise ValueError("Activity options require nonempty category and option strings")
            entries.extend(_scalar(STRING, category) + _word(EQUAL) + _scalar(STRING, choice))
            if len(entries) > MAX_MESSAGE_SIZE - 16:
                raise ValueError("Activity options exceed the PDX frame buffer")
        body.extend(_property(0x3641, _word(OPEN) + bytes(entries) + _word(CLOSE)))
    phases = plan.get("phases", [])
    if not isinstance(phases, list) or len(phases) > MAX_MESSAGE_SIZE // 16:
        raise ValueError("Activity phases must be a bounded array")
    if phases:
        records = bytearray()
        for item in phases:
            if (not isinstance(item, dict)
                    or set(item) - {"phase", "province", "guest_subsets"}
                    or not {"phase", "province"} <= set(item)):
                raise ValueError("Each activity phase requires phase and province")
            if not isinstance(item["phase"], str) or not item["phase"]:
                raise ValueError("Activity phase key must be a nonempty string")
            entry = bytearray()
            entry.extend(_property(0x2971, _scalar(STRING, item["phase"])))
            entry.extend(_property(0x27D2, _scalar(I32, item["province"])))
            subsets = item.get("guest_subsets", [])
            if not isinstance(subsets, list):
                raise ValueError("Phase guest_subsets must be an array")
            if subsets:
                subset_records = bytearray()
                for subset in subsets:
                    if (not isinstance(subset, dict)
                            or set(subset) - {"key", "guests"}
                            or "key" not in subset):
                        raise ValueError("Each guest subset requires key and optional guests")
                    if not isinstance(subset["key"], str) or not subset["key"]:
                        raise ValueError("Guest subset key must be a nonempty string")
                    sub_entry = bytearray(_property(0xDC, _scalar(STRING, subset["key"])))
                    guests = subset.get("guests", [])
                    encoded_guests = _scalar(U32_LIST, guests)
                    if guests:
                        sub_entry.extend(_property(0x3307, encoded_guests))
                    subset_records.extend(_word(OPEN) + bytes(sub_entry) + _word(CLOSE))
                entry.extend(_property(0x3819, _word(OPEN) + bytes(subset_records) + _word(CLOSE)))
            records.extend(_word(OPEN) + bytes(entry) + _word(CLOSE))
        body.extend(_property(0x3642, _word(OPEN) + bytes(records) + _word(CLOSE)))
    special_guests = plan.get("special_guests", {})
    if not isinstance(special_guests, dict):
        raise ValueError("Activity special_guests require a slot-to-character JSON object")
    if special_guests:
        entries = bytearray()
        for slot, character in special_guests.items():
            if not isinstance(slot, str) or not slot:
                raise ValueError("Special guest slot names must be nonempty strings")
            entries.extend(_scalar(STRING, slot) + _word(EQUAL) + _scalar(U32, character))
            if len(entries) > MAX_MESSAGE_SIZE - 16:
                raise ValueError("Special guests exceed the PDX frame buffer")
        body.extend(_property(0x3613, _word(OPEN) + bytes(entries) + _word(CLOSE)))
    body.extend(_property(0x2E52, _bucket_array(plan.get("invitations", []), U32, "invitations")))
    travel_plan = plan.get("travel_plan")
    if travel_plan is not None:
        body.extend(_property(0x37B6, _travel_plan(travel_plan)))
    variables = plan.get("variables", {})
    if not isinstance(variables, dict):
        raise ValueError("Activity variables require a named JSON object")
    if variables:
        entries = bytearray()
        for name, value in variables.items():
            if not isinstance(name, str) or not name:
                raise ValueError("Activity variable names must be nonempty strings")
            entries.extend(_scalar(STRING, name) + _word(EQUAL) + _scope_value(value))
            if len(entries) > MAX_MESSAGE_SIZE - 16:
                raise ValueError("Activity variables exceed the PDX frame buffer")
        body.extend(_property(0x555, _word(OPEN) + bytes(entries) + _word(CLOSE)))
    for field, token in (("tenets", 0x3A63), ("doctrines", 0x3E4E)):
        values = plan.get(field, [])
        encoded = _scalar(STRING_LIST, values)
        if values:
            body.extend(_property(token, encoded))
    return _word(OPEN) + bytes(body) + _word(CLOSE)


def _normalize_bucket_array(raw, name):
    if not isinstance(raw, dict) or "size" not in raw or type(raw["size"]) is not int or raw["size"] < 0:
        raise ValueError(f"Invalid {name} bucket size")
    size = raw["size"]
    if size > MAX_MESSAGE_SIZE // 8:
        raise ValueError(f"{name} exceeds the maximum bucket count")
    buckets = [[] for _ in range(size)]
    for key, values in raw.items():
        if key == "size":
            continue
        if type(key) is not int or not 0 <= key < size:
            raise ValueError(f"Invalid {name} bucket index")
        if values == {}:
            buckets[key] = []
        elif isinstance(values, list) and all(isinstance(item, dict) and set(item) == {"value"} for item in values):
            buckets[key] = [item["value"] for item in values]
        else:
            raise ValueError(f"Invalid {name} bucket items")
    return buckets


def _normalize_travel_plan(plan):
    if not isinstance(plan, dict):
        return
    if "companions" in plan:
        plan["companions"] = [item["value"] for item in plan["companions"]]
    for field in ("options", "destinations"):
        if field in plan:
            plan[field] = [item["value"] for item in plan[field]]
    if "waypoints" in plan:
        plan["waypoints"] = _normalize_bucket_array(plan["waypoints"], "waypoints")
    if "modifier" in plan and isinstance(plan["modifier"], dict):
        plan["modifier"] = [plan["modifier"]]


def _normalize_activity_plan(plan):
    if not isinstance(plan, dict):
        return
    if "phases" in plan:
        phases = [item["value"] for item in plan["phases"]]
        for phase in phases:
            if isinstance(phase, dict) and "guest_subsets" in phase:
                subsets = [item["value"] for item in phase["guest_subsets"]]
                for subset in subsets:
                    if isinstance(subset, dict) and "guests" in subset:
                        subset["guests"] = [item["value"] for item in subset["guests"]]
                phase["guest_subsets"] = subsets
        plan["phases"] = phases
    if "invitations" in plan:
        plan["invitations"] = _normalize_bucket_array(plan["invitations"], "invitations")
    if "travel_plan" in plan:
        _normalize_travel_plan(plan["travel_plan"])
    for field in ("tenets", "doctrines"):
        if field in plan:
            plan[field] = [item["value"] for item in plan[field]]


def travel_plan_fields(departure_date, departure_location, *, owner=0xFFFFFFFF, travel_leader=0xFFFFFFFF,
                       companions=None, activity=0xFFFFFFFF, task_contract=0xFFFFFFFF, options=None,
                       destinations=None, path_duration=-1, max_path_duration=-1,
                       travel_with_domicile=False, migration=False, can_cancel_planning=True,
                       waypoints=None, modifier=None, on_arrival=None, on_start=None,
                       on_travel_planner_cancel=None, on_arrival_destinations="all_but_last", scope=None):
    """Build a native CTravelPlan dictionary for start_activity_command or join_activity_command."""
    return {
        "owner": owner, "travel_leader": travel_leader,
        "companions": [] if companions is None else companions,
        "activity": activity, "task_contract": task_contract,
        "options": [] if options is None else options,
        "departure_date": departure_date, "departure_location": departure_location,
        "destinations": [] if destinations is None else destinations,
        "path_duration": path_duration, "max_path_duration": max_path_duration,
        "travel_with_domicile": travel_with_domicile, "migration": migration,
        "can_cancel_planning": can_cancel_planning,
        "waypoints": [] if waypoints is None else waypoints,
        "modifier": [] if modifier is None else modifier,
        "on_arrival": {} if on_arrival is None else on_arrival,
        "on_start": {} if on_start is None else on_start,
        "on_travel_planner_cancel": {} if on_travel_planner_cancel is None else on_travel_planner_cancel,
        "on_arrival_destinations": on_arrival_destinations,
        "scope": {"root": {}, "seed": -1} if scope is None else scope,
    }


def activity_plan_fields(activity_type, host, start_date, phases, *, intent=None,
                         guest_delay_days=0, invite_duration=0, options=None,
                         special_guests=None, invitations=None, travel_plan=None,
                         variables=None, tenets=None, doctrines=None):
    """Build native CActivityPlan fields for start_activity_command."""
    if not isinstance(activity_type, str) or not activity_type:
        raise ValueError("Starting an activity requires a nonempty activity type")
    if not isinstance(phases, list) or not phases:
        raise ValueError("Starting an activity requires at least one phase")
    plan = {
        "type": activity_type,
        "host": host,
        "intent": {} if intent is None else intent,
        "start_date": start_date,
        "guest_delay_days": guest_delay_days,
        "invite_duration": invite_duration,
        "options": {} if options is None else options,
        "phases": phases,
        "special_guests": {} if special_guests is None else special_guests,
        "invitations": [] if invitations is None else invitations,
        "variables": {} if variables is None else variables,
        "tenets": [] if tenets is None else tenets,
        "doctrines": [] if doctrines is None else doctrines,
    }
    if travel_plan is not None:
        plan["travel_plan"] = travel_plan
    return {"data": plan}


class JominiCodec:
    def __init__(self, token_file=DEFAULT_TOKENS):
        schema = json.loads(Path(token_file).read_text("utf-8"))
        if schema.get("exe_sha256") != SUPPORTED_EXE_SHA256:
            raise ValueError("Token dictionary does not match this command schema")
        self.tokens = {int(key, 0): value for key, value in schema["tokens"].items()}
        self.game_version = schema["game_version"]
        for name, command in COMMAND_SCHEMAS.items():
            if self.tokens.get(command["token"]) != name:
                raise ValueError("Command schema and token dictionary disagree")

    def encode_command(self, name, fields, header):
        if name not in COMMAND_SCHEMAS:
            raise ValueError("Unsupported command encoder, use the supported subcommand to list schemas")
        if not isinstance(fields, dict) or not isinstance(header, dict):
            raise ValueError("Command fields and header must be JSON objects")
        schema = COMMAND_SCHEMAS[name]
        allowed = {field[0] for field in schema["fields"]}
        if set(fields) - allowed:
            raise ValueError("Unknown fields for this command")
        if set(header) - {field[0] for field in HEADER_FIELDS}:
            raise ValueError("Unknown command header fields")
        header = {"attribs": 8, **header}
        if type(header["attribs"]) is not int or not 0 <= header["attribs"] <= 255:
            raise ValueError("Command attribs must fit the observed byte-sized field")
        common = bytearray()
        for field, token, tag in HEADER_FIELDS:
            if field not in header:
                raise ValueError(f"Command header requires {field}")
            common.extend(_property(token, _scalar(tag, header[field])))
        body = bytearray()
        for field, token, tag, default in schema["fields"]:
            if field not in fields and field in schema.get("optional_fields", ()):
                continue
            if field not in fields and default is None:
                raise ValueError(f"Command requires {field}")
            value = fields.get(field, default)
            if tag in (TOKEN_VALUE, I32_ENUM):
                options = schema["enums"][field]
                if not isinstance(value, str) or value not in options:
                    raise ValueError(f"Invalid {field}. Choose one of {', '.join(options)}")
                encoded = _scalar(I32 if tag == I32_ENUM else tag, options[value])
            else:
                encoded = _scalar(tag, value)
            # Native writers omit the schema's explicit optional defaults.
            if default is not None and value == default:
                continue
            if tag == BLOB:
                body.extend(_word(token) + encoded)
            else:
                body.extend(_property(token, encoded))
        contents = _property(0x1F3, _word(OPEN) + common + _word(CLOSE))
        contents += _property(0x41, _word(OPEN) + body + _word(CLOSE))
        result = _property(schema["token"], _word(OPEN) + contents + _word(CLOSE))
        if len(result) > MAX_MESSAGE_SIZE - 60:
            raise ValueError("Command will not fit in a PDX frame")
        return result

    def decode_command(self, payload):
        document = self.decode_document(payload)
        if len(document) != 1 or "key" not in document[0]:
            raise ValueError("A command must contain one top-level named object")
        command = document[0]
        if command["value"]["kind"] != "object":
            raise ValueError("Command contents must be an object")
        sections = _object_values(command["value"])
        if not isinstance(sections, dict):
            raise ValueError("Command sections must be named properties")
        fields = sections.get("object")
        schema = COMMAND_SCHEMAS.get(command["key"]["value"])
        if schema and isinstance(fields, dict):
            for field, _, tag, _ in schema["fields"]:
                if tag == I32_ENUM and type(fields.get(field)) is int:
                    names = {value: name for name, value in schema["enums"][field].items()}
                    fields[field] = names.get(fields[field], fields[field])
                if tag == INTERACTION_DATA and field in fields:
                    _normalize_character_interaction(fields[field])
                if tag == TRAVEL_PLAN and field in fields:
                    _normalize_travel_plan(fields[field])
                if tag == ACTIVITY_PLAN and field in fields:
                    _normalize_activity_plan(fields[field])
                if (tag not in LIST_TYPES and tag != RALLY_POINTS) or field not in fields:
                    continue
                values = fields[field]
                if values == {}:
                    fields[field] = []
                elif isinstance(values, list) and all(
                    isinstance(item, dict) and set(item) == {"value"} for item in values
                ):
                    fields[field] = [item["value"] for item in values]
                else:
                    raise ValueError(f"Expected an anonymous array for {field}")
        result = {
            "type": command["key"]["value"], "type_token": command["key"].get("token"),
            "default_state": sections.get("default_state"), "fields": fields,
            "document": document,
        }
        return result

    def decode_document(self, payload, *, max_size=MAX_MESSAGE_SIZE):
        return _BinaryParser(payload, self.tokens, max_size=max_size).parse()

    def decode_pdx_message(self, message, *, from_server=True):
        payload = bytes.fromhex(message["payload_hex"])
        channel = message["word_8"]
        if channel == 0:
            return {"channel": "command", **self.decode_command(payload)}
        if channel == 1:
            return {"channel": "session", **decode_session_message(payload, from_server=from_server)}
        return {"channel": "unknown", "channel_id": channel, "payload_hex": payload.hex()}


def _object_values(node):
    if node["kind"] != "object":
        return node["value"]
    if any("key" not in item for item in node["items"]):
        return [{
            **({"key": item["key"]["value"]} if "key" in item else {}),
            "value": _object_values(item["value"]),
        } for item in node["items"]]
    result = {}
    repeated = set()
    for item in node["items"]:
        key = item["key"]["value"]
        value = _object_values(item["value"])
        if key in repeated:
            result[key].append(value)
        elif key in result:
            result[key] = [result[key], value]
            repeated.add(key)
        else:
            result[key] = value
    return result


class _BinaryParser:
    def __init__(self, payload, tokens, *, max_size=MAX_MESSAGE_SIZE):
        if len(payload) > max_size:
            raise ValueError("Binary document exceeds the configured byte limit")
        self.data = payload
        self.position = 0
        self.tokens = tokens

    def _take(self, size):
        end = self.position + size
        if end > len(self.data):
            raise ValueError(f"Truncated binary token at offset {self.position}")
        result = self.data[self.position:end]
        self.position = end
        return result

    def _peek(self):
        if self.position == len(self.data):
            return None
        if self.position + 2 > len(self.data):
            raise ValueError("Truncated binary token ID")
        return struct.unpack_from("<H", self.data, self.position)[0]

    def _value(self, depth):
        offset = self.position
        token = struct.unpack("<H", self._take(2))[0]
        if token == OPEN:
            if depth >= 128:
                raise ValueError("Binary command nesting exceeds the parser limit")
            return {"kind": "object", "offset": offset, "items": self._items(depth + 1, nested=True)}
        if token in (I32, U32):
            value = struct.unpack("<i" if token == I32 else "<I", self._take(4))[0]
            return {"kind": "i32" if token == I32 else "u32", "value": value, "offset": offset}
        if token == U64:
            return {"kind": "u64", "value": struct.unpack("<Q", self._take(8))[0], "offset": offset}
        if token == F32:
            raw = self._take(4)
            return {"kind": "f32", "value": struct.unpack("<f", raw)[0], "raw_hex": raw.hex(), "offset": offset}
        if token == FIXED_POINT:
            raw = struct.unpack("<q", self._take(8))[0]
            return {"kind": "fixed_point", "value": raw / 100000, "raw_integer": raw, "scale": 100000, "offset": offset}
        if token == BOOL:
            value = self._take(1)[0]
            if value not in (0, 1):
                raise ValueError(f"Invalid boolean byte at offset {offset}")
            return {"kind": "bool", "value": bool(value), "offset": offset}
        if token in (STRING, UNQUOTED_STRING):
            size = struct.unpack("<H", self._take(2))[0]
            raw = self._take(size)
            return {
                "kind": "string", "value": raw.decode("utf-8", errors="replace"),
                "raw_hex": raw.hex(), "offset": offset,
            }
        if token == BLOB:
            size = struct.unpack("<I", self._take(4))[0]
            raw = self._take(size)
            return {"kind": "binary", "value": {"hex": raw.hex()}, "offset": offset}
        if token == RGB and self._peek() == OPEN:
            return {"kind": "rgb", "value": self._value(depth), "offset": offset}
        if token in (EQUAL, CLOSE) or token < 0x16 and token not in self.tokens:
            raise ValueError(f"Unsupported or unexpected binary token {token:#x} at {offset}")
        return {
            "kind": "token", "token": hex(token), "offset": offset,
            "value": self.tokens.get(token, f"token_{token:#06x}"),
        }

    def _items(self, depth, nested=False):
        items = []
        while self._peek() is not None:
            if self._peek() == CLOSE:
                if not nested:
                    raise ValueError("Unexpected closing object token")
                self.position += 2
                return items
            value = self._value(depth)
            if value["kind"] == "token" and value.get("token") == "0xf0" and self._peek() == BLOB:
                # Binary CWriter::WriteData emits data + blob without '='.
                items.append({"key": value, "value": self._value(depth)})
            elif self._peek() == EQUAL:
                if value["kind"] not in ("token", "string", "i32", "u32", "u64"):
                    raise ValueError("A binary property key must be a token, string or integer")
                self.position += 2
                items.append({"key": value, "value": self._value(depth)})
            else:
                items.append({"value": value})
        if nested:
            raise ValueError("Missing closing object token")
        return items

    def parse(self):
        return self._items(0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    decode = commands.add_parser("decode")
    decode.add_argument("payload", type=Path)
    encode = commands.add_parser("encode")
    encode.add_argument("spec", type=Path)
    encode.add_argument("output", type=Path)
    decode_session = commands.add_parser("decode-session")
    decode_session.add_argument("payload", type=Path)
    decode_session.add_argument("--from-client", action="store_true")
    encode_session = commands.add_parser("encode-session")
    encode_session.add_argument("spec", type=Path)
    encode_session.add_argument("output", type=Path)
    commands.add_parser("supported")
    commands.add_parser("supported-session")
    args = parser.parse_args()
    codec = JominiCodec()
    if args.action == "supported":
        print(json.dumps(COMMAND_SCHEMAS, indent=2))
    elif args.action == "supported-session":
        print(json.dumps(SESSION_SCHEMAS, indent=2))
    elif args.action == "decode":
        print(json.dumps(codec.decode_command(args.payload.read_bytes()), indent=2))
    elif args.action == "decode-session":
        print(json.dumps(decode_session_message(args.payload.read_bytes(), from_server=not args.from_client), indent=2))
    else:
        spec = json.loads(args.spec.read_text("utf-8-sig"))
        if args.action == "encode-session":
            payload = encode_session_message(spec["type"], spec["fields"], trailing_hex=spec.get("trailing_hex"))
        else:
            payload = codec.encode_command(spec["type"], spec["fields"], spec["header"])
        args.output.write_bytes(payload)
        print(json.dumps({"bytes": len(payload), "output": str(args.output.resolve())}))


if __name__ == "__main__":
    main()
