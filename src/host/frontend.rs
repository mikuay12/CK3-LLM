use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail, ensure};

use crate::settings::replace_file;

const LOGGED_IN: &str = "visible = \"[And( StatusIsLoggedIn, Not( StatusIsUserNameEmpty ) )]\"";
const LOGGED_OUT: &str = "visible = \"[Or( Not( StatusIsLoggedIn ), StatusIsUserNameEmpty )]\"";

pub struct Frontend {
    path: PathBuf,
    original: Vec<u8>,
    patched: Vec<u8>,
    active: bool,
}

impl Frontend {
    pub fn apply(path: PathBuf, capture: &Path) -> Result<Self> {
        let current =
            fs::read(&path).with_context(|| format!("读取 CK3 菜单文件 {}", path.display()))?;
        let backup = path.with_file_name("frontend_main.gui.ck3_llm_original");
        let legacy = path.with_file_name("frontend_main.gui.local_bridge_orig");
        let (original, patched) = match patched(&current) {
            Ok(output) => (current, output),
            Err(error) => [backup.as_path(), legacy.as_path()]
                .into_iter()
                .find_map(|candidate| {
                    let recovered = fs::read(candidate).ok()?;
                    let output = patched(&recovered).ok()?;
                    (current == output).then_some((recovered, output))
                })
                .ok_or(error)?,
        };

        replace_file(&capture.join("frontend_main.original.gui"), &original)
            .context("保存 CK3 菜单备份")?;
        replace_file(&backup, &original).context("保存安装目录中的菜单恢复文件")?;
        replace_file(&path, &patched).context("应用 CK3 本地菜单")?;
        Ok(Self {
            path,
            original,
            patched,
            active: true,
        })
    }

    pub fn restore(&mut self) -> Result<()> {
        if !self.active {
            return Ok(());
        }
        let current = fs::read(&self.path).context("检查 CK3 菜单当前状态")?;
        if current != self.original && current != self.patched {
            self.active = false;
            bail!("CK3 菜单文件已从外部变更，原始备份保存在本次会话目录");
        }
        if current == self.patched {
            replace_file(&self.path, &self.original).context("恢复原始 CK3 菜单")?;
        }
        self.active = false;
        Ok(())
    }
}

impl Drop for Frontend {
    fn drop(&mut self) {
        let _ = self.restore();
    }
}

fn patched(original: &[u8]) -> Result<Vec<u8>> {
    let text = str::from_utf8(original).context("CK3 菜单文件编码无效")?;
    let text = text.strip_prefix('\u{feff}').unwrap_or(text);
    ensure!(
        text.matches(LOGGED_IN).count() == 1 && text.matches(LOGGED_OUT).count() == 1,
        "CK3 菜单结构与本地联机版本不兼容"
    );
    Ok(text
        .replace(LOGGED_IN, "visible = yes")
        .replace(LOGGED_OUT, "visible = no")
        .into_bytes())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn restores_exact_original_and_recovers_an_interrupted_patch() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("frontend_main.gui");
        let original = format!("\u{feff}{LOGGED_IN}\r\n{LOGGED_OUT}\r\n");
        fs::write(&path, &original).unwrap();
        let capture = directory.path().join("capture");
        let mut patch = Frontend::apply(path.clone(), &capture).unwrap();
        assert_eq!(
            fs::read(&path).unwrap(),
            b"visible = yes\r\nvisible = no\r\n"
        );
        patch.active = false;
        drop(patch);
        let mut recovered = Frontend::apply(path.clone(), &capture).unwrap();
        recovered.restore().unwrap();
        assert_eq!(fs::read(path).unwrap(), original.as_bytes());
    }
}
