/* Token-boundary indexing for the version-verified Jomini binary world. */
#include <stdint.h>

static uint16_t read16(const uint8_t *data, uint32_t position) {
    return data[position] | ((uint16_t)data[position + 1] << 8);
}

static uint32_t read32(const uint8_t *data, uint32_t position) {
    return read16(data, position) | ((uint32_t)read16(data, position + 2) << 16);
}

static int next_token(const uint8_t *data, uint32_t limit, uint32_t *position) {
    uint32_t start = *position;
    uint32_t size = 0;
    uint16_t tag;
    if (start > limit || limit - start < 2) return -1;
    tag = read16(data, start);
    start += 2;
    switch (tag) {
        case 0x0c: case 0x0d: case 0x14: size = 4; break;
        case 0x0e: size = 1; break;
        case 0x167: case 0x29c: size = 8; break;
        case 0x0f: case 0x17:
            if (limit - start < 2) return -1;
            size = read16(data, start);
            start += 2;
            break;
        case 0x1fc:
            if (limit - start < 4) return -1;
            size = read32(data, start);
            start += 4;
            break;
        default:
            if (tag < 0x16 && tag != 0 && tag != 1 && tag != 3 &&
                tag != 4 && tag != 0x0b && tag != 0x15) return -2;
    }
    if (size > limit - start) return -1;
    *position = start + size;
    return tag;
}

static int skip_value(const uint8_t *data, uint32_t limit, uint32_t *position) {
    int tag = next_token(data, limit, position);
    unsigned int depth = 1;
    if (tag < 0) return tag;
    if (tag == 1 || tag == 4) return -3;
    if (tag == 0x243 && *position + 2 <= limit && read16(data, *position) == 3) {
        tag = next_token(data, limit, position);
    }
    if (tag != 3) return 0;
    while (depth) {
        tag = next_token(data, limit, position);
        if (tag < 0) return tag;
        if (tag == 3 && ++depth > 128) return -4;
        if (tag == 4) --depth;
    }
    return 0;
}

/* Each result row is key_start, key_end, value_start, value_end. */
int index_world(const uint8_t *data, uint32_t size, uint32_t start,
                uint32_t end, int wrapped, uint32_t *rows,
                uint32_t capacity, uint32_t *error_position) {
    uint32_t position = start;
    uint32_t limit = end;
    uint32_t count = 0;
    if (end > size || start > end) return -1;
    if (wrapped) {
        if (end - start < 4 || read16(data, start) != 3 || read16(data, end - 2) != 4) return -5;
        position += 2;
        limit -= 2;
    }
    while (position < limit) {
        uint32_t key_start = position;
        uint32_t key_end, value_start;
        int status;
        *error_position = position;
        status = next_token(data, limit, &position);
        if (status < 0) return status;
        key_end = position;
        if (limit - position < 2 || read16(data, position) != 1) return -6;
        position += 2;
        value_start = position;
        status = skip_value(data, limit, &position);
        if (status < 0) return status;
        if (rows) {
            if (count >= capacity) return -7;
            rows[count * 4] = key_start;
            rows[count * 4 + 1] = key_end;
            rows[count * 4 + 2] = value_start;
            rows[count * 4 + 3] = position;
        }
        ++count;
    }
    return count;
}

int find_token_key(const uint8_t *data, const uint32_t *rows,
                   uint32_t count, uint32_t token) {
    uint32_t index;
    for (index = 0; index < count; ++index) {
        uint32_t start = rows[index * 4];
        if (rows[index * 4 + 1] - start == 2 && read16(data, start) == token) return index;
    }
    return -1;
}
