use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use anyhow::{Context, Result, ensure};
use tempfile::NamedTempFile;
use uuid::Uuid;

use crate::world::World;

use super::game_protocol::{Incoming, TransferInfo};

pub(super) const ANNOUNCE: u16 = 0x6a0;
pub(super) const FAST_HANDSHAKE: u16 = 0x619;
pub(super) const FAST_CHUNK: u16 = 0x61b;
pub(super) const SERIAL_METADATA: u16 = 0x6a2;
pub(super) const SERIAL_CHUNK: u16 = 0x6d7;
const MAX_FILE_BYTES: u32 = 1 << 30;
const MAX_CHUNK_BYTES: u32 = 1 << 20;
const REPORT_PERIOD: Duration = Duration::from_millis(300);

#[derive(PartialEq, Eq)]
enum Layout {
    Fast { chunk_size: u32, count: u32 },
    Serial(Vec<Chunk>),
}

#[derive(PartialEq, Eq)]
struct Chunk {
    offset: u32,
    size: u32,
    checksum: i32,
}

impl Layout {
    fn count(&self) -> u32 {
        match self {
            Self::Fast { count, .. } => *count,
            Self::Serial(chunks) => chunks.len() as u32,
        }
    }

    fn at(&self, chunk: u32, file_size: u32) -> Result<(u32, u32, Option<i32>)> {
        ensure!(chunk < self.count(), "快照分块编号越界");
        Ok(match self {
            Self::Fast { chunk_size, .. } => {
                let offset = chunk * chunk_size;
                (offset, (*chunk_size).min(file_size - offset), None)
            }
            Self::Serial(chunks) => {
                let chunk = &chunks[chunk as usize];
                (chunk.offset, chunk.size, Some(chunk.checksum))
            }
        })
    }
}

pub(super) struct Transfer {
    pub info: TransferInfo,
    pub peer: i32,
    layout: Option<Layout>,
    bitmap: Vec<u8>,
    received: u32,
    file: Option<NamedTempFile>,
    last: Instant,
}

pub(super) struct Status {
    pub info: TransferInfo,
    pub peer: i32,
    pub bitmap: Vec<u8>,
    pub received: u32,
    pub total: u32,
    pub file: Option<NamedTempFile>,
}

impl Transfer {
    pub fn announced(info: TransferInfo, peer: i32) -> Self {
        Self {
            info,
            peer,
            layout: None,
            bitmap: Vec::new(),
            received: 0,
            file: None,
            last: Instant::now(),
        }
    }

    pub fn initialize(&mut self, directory: &Path, chunk_size: u32) -> Result<bool> {
        ensure!(
            (1..=MAX_CHUNK_BYTES).contains(&chunk_size),
            "快照分块大小无效"
        );
        let layout = Layout::Fast {
            chunk_size,
            count: self.info.size.div_ceil(chunk_size),
        };
        self.prepare(directory, layout)
    }

    pub fn initialize_serial(&mut self, directory: &Path, command: &Incoming<'_>) -> Result<bool> {
        let sizes = command.list("sizes", |value| {
            Ok(u32::try_from(
                value.as_u64().context("分块长度需要非负整数")?,
            )?)
        })?;
        let checksums = command.list("checksums", |value| {
            Ok(i32::try_from(
                value.as_i64().context("分块校验值需要整数")?,
            )?)
        })?;
        ensure!(
            sizes.len() == checksums.len(),
            "串行分块长度和校验值数量不一致"
        );
        let mut offset = 0_u32;
        let mut chunks = Vec::with_capacity(sizes.len());
        for (size, checksum) in sizes.into_iter().zip(checksums) {
            ensure!(
                (1..=MAX_CHUNK_BYTES).contains(&size) && size <= self.info.size - offset,
                "串行分块长度与文件长度不一致"
            );
            chunks.push(Chunk {
                offset,
                size,
                checksum,
            });
            offset += size;
        }
        ensure!(offset == self.info.size, "串行分块长度总和与文件长度不一致");
        self.prepare(directory, Layout::Serial(chunks))
    }

    fn prepare(&mut self, directory: &Path, layout: Layout) -> Result<bool> {
        if let Some(current) = &self.layout {
            ensure!(current == &layout, "快照分块信息中途改变");
            return Ok(false);
        }
        ensure!(
            (1..=MAX_FILE_BYTES).contains(&self.info.size),
            "快照文件长度无效"
        );
        let length = layout.count().div_ceil(8) as usize;
        ensure!(length <= (1 << 20) - 512, "快照分块位图过大");
        let file = NamedTempFile::new_in(directory)?;
        file.as_file().set_len(u64::from(self.info.size))?;
        self.layout = Some(layout);
        self.bitmap.resize(length, 0);
        self.file = Some(file);
        self.last = Instant::now();
        Ok(true)
    }

    pub fn chunk(&mut self, chunk: u32, data: &[u8]) -> Result<()> {
        let Some(file) = &mut self.file else {
            return Ok(());
        };
        let (offset, size, checksum) = self
            .layout
            .as_ref()
            .expect("initialized file")
            .at(chunk, self.info.size)?;
        ensure!(size as usize == data.len(), "快照分块长度不匹配");
        if let Some(checksum) = checksum {
            ensure!(Checksum::of(data) == checksum, "串行快照分块校验失败");
        }
        file.as_file_mut()
            .seek(SeekFrom::Start(u64::from(offset)))?;
        let bitmap = &mut self.bitmap[(chunk / 8) as usize];
        let mask = 1 << (chunk % 8);
        if *bitmap & mask != 0 {
            let mut existing = vec![0; data.len()];
            file.as_file_mut().read_exact(&mut existing)?;
            ensure!(existing == data, "房主重传的分块内容冲突");
        } else {
            file.as_file_mut().write_all(data)?;
            *bitmap |= mask;
            self.received += 1;
        }
        Ok(())
    }

    pub fn take_complete(&mut self) -> Option<NamedTempFile> {
        if self
            .layout
            .as_ref()
            .is_some_and(|layout| self.received == layout.count())
        {
            self.file.take()
        } else {
            None
        }
    }

    pub fn progress(&self) -> Option<(u32, u32)> {
        Some((self.received, self.layout.as_ref()?.count()))
    }

    pub fn next_missing(&self) -> Option<u32> {
        let (_, count) = self.progress()?;
        self.bitmap.iter().enumerate().find_map(|(index, &byte)| {
            let first = index as u32 * 8 + byte.trailing_ones();
            (byte != u8::MAX && first < count).then_some(first)
        })
    }

    pub fn status(&mut self) -> Option<Status> {
        self.file.as_ref()?;
        let total = self.layout.as_ref()?.count();
        if self.received != total && self.last.elapsed() < REPORT_PERIOD {
            return None;
        }
        self.last = Instant::now();
        Some(Status {
            info: self.info.clone(),
            peer: self.peer,
            bitmap: self.bitmap.clone(),
            received: self.received,
            total,
            file: self.take_complete(),
        })
    }
}

pub(super) fn verify(
    file: NamedTempFile,
    directory: &Path,
    info: &TransferInfo,
) -> Result<PathBuf> {
    let mut handle = file.reopen()?;
    let mut disk = Checksum::default();
    let mut buffer = vec![0; 256 * 1024];
    loop {
        let amount = handle.read(&mut buffer)?;
        if amount == 0 {
            break;
        }
        disk.feed(&buffer[..amount]);
    }
    drop(handle);
    let snapshot = info.filename.starts_with("hotjoin_");
    if snapshot {
        let world = World::open(file.path())?;
        ensure!(
            disk.value() == info.checksum || Checksum::of(world.binary()) == info.checksum,
            "热加入快照校验失败"
        );
        world.summary()?;
    } else {
        ensure!(disk.value() == info.checksum, "传输文件校验失败");
    }
    let prefix = if snapshot { "world" } else { "transfer" };
    let path = directory.join(format!("{prefix}-{}.bin", Uuid::new_v4()));
    file.persist(&path)?;
    Ok(path)
}

#[derive(Default)]
pub(super) struct Checksum {
    first: u32,
    second: u32,
}

impl Checksum {
    pub fn of(bytes: &[u8]) -> i32 {
        let mut state = Self::default();
        state.feed(bytes);
        state.value()
    }

    pub fn feed(&mut self, bytes: &[u8]) {
        for &byte in bytes {
            self.first += u32::from(byte);
            if self.first > 65_535 {
                self.first -= 65_535;
            }
            self.second += self.first;
            if self.second > 65_535 {
                self.second -= 65_535;
            }
        }
    }

    pub fn value(&self) -> i32 {
        ((self.first << 16) | self.second) as i32
    }
}
