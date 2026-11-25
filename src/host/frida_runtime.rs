use std::ffi::{CStr, CString, c_char, c_void};
use std::ptr::{self, NonNull};
use std::sync::Once;
use std::sync::mpsc::Sender;

use anyhow::{Context, Result, anyhow, ensure};
use frida_sys as sys;
use serde_json::Value;

static FRIDA: Once = Once::new();

struct Handle<T>(NonNull<T>);

impl<T> Handle<T> {
    fn new(pointer: *mut T) -> Result<Self> {
        Ok(Self(NonNull::new(pointer).context("Frida 返回空指针")?))
    }

    fn raw(&self) -> *mut T {
        self.0.as_ptr()
    }
}

impl<T> Drop for Handle<T> {
    fn drop(&mut self) {
        unsafe { sys::frida_unref(self.raw().cast()) };
    }
}

fn call<T>(operation: impl FnOnce(*mut *mut sys::GError) -> T) -> Result<T> {
    let mut error = ptr::null_mut();
    let output = operation(&mut error);
    if error.is_null() {
        return Ok(output);
    }
    let message = unsafe {
        CStr::from_ptr((*error).message)
            .to_string_lossy()
            .into_owned()
    };
    unsafe { sys::g_error_free(error) };
    Err(anyhow!(message))
}

pub struct Device {
    device: Handle<sys::FridaDevice>,
    manager: Handle<sys::FridaDeviceManager>,
}

impl Device {
    pub fn local(expected_version: &str) -> Result<Self> {
        FRIDA.call_once(|| unsafe { sys::frida_init() });
        let version = unsafe { CStr::from_ptr(sys::frida_version_string()) }.to_string_lossy();
        ensure!(
            version == expected_version,
            "Frida 版本为 {version}，需要 {expected_version}"
        );
        let manager = Handle::new(unsafe { sys::frida_device_manager_new() })?;
        let local = call(|error| unsafe {
            sys::frida_device_manager_get_device_by_type_sync(
                manager.raw(),
                sys::FridaDeviceType_FRIDA_DEVICE_TYPE_LOCAL,
                0,
                ptr::null_mut(),
                error,
            )
        })?;
        Ok(Self {
            device: Handle::new(local)?,
            manager,
        })
    }

    pub fn ck3_running(&self) -> Result<bool> {
        let processes = Handle::new(call(|error| unsafe {
            sys::frida_device_enumerate_processes_sync(
                self.device.raw(),
                ptr::null_mut(),
                ptr::null_mut(),
                error,
            )
        })?)?;
        for index in 0..unsafe { sys::frida_process_list_size(processes.raw()) } {
            let process =
                Handle::new(unsafe { sys::frida_process_list_get(processes.raw(), index) })?;
            let name = unsafe { CStr::from_ptr(sys::frida_process_get_name(process.raw())) }
                .to_string_lossy();
            if name.eq_ignore_ascii_case("ck3") || name.eq_ignore_ascii_case("ck3.exe") {
                return Ok(true);
            }
        }
        Ok(false)
    }

    pub fn spawn(&self, executable: &str, cwd: &str, arguments: &[&str]) -> Result<u32> {
        let executable = CString::new(executable)?;
        let cwd = CString::new(cwd)?;
        let arguments = arguments
            .iter()
            .copied()
            .map(CString::new)
            .collect::<Result<Vec<_>, _>>()?;
        let mut argv: Vec<_> = arguments
            .iter()
            .map(|arg| arg.as_ptr().cast_mut())
            .collect();
        let options = Handle::new(unsafe { sys::frida_spawn_options_new() })?;
        unsafe {
            sys::frida_spawn_options_set_cwd(options.raw(), cwd.as_ptr());
            sys::frida_spawn_options_set_argv(options.raw(), argv.as_mut_ptr(), argv.len() as i32);
        }
        let pid = call(|error| unsafe {
            sys::frida_device_spawn_sync(
                self.device.raw(),
                executable.as_ptr(),
                options.raw(),
                ptr::null_mut(),
                error,
            )
        })?;
        ensure!(pid != 0, "Frida 返回了无效 CK3 PID");
        Ok(pid)
    }

    pub fn attach(&self, pid: u32) -> Result<Session> {
        let session = call(|error| unsafe {
            sys::frida_device_attach_sync(
                self.device.raw(),
                pid,
                ptr::null_mut(),
                ptr::null_mut(),
                error,
            )
        })?;
        Ok(Session(Handle::new(session)?))
    }

    pub fn resume(&self, pid: u32) -> Result<()> {
        call(|error| unsafe {
            sys::frida_device_resume_sync(self.device.raw(), pid, ptr::null_mut(), error)
        })
    }

    pub fn cancel_spawn(&self, pid: u32) {
        let _ = call(|error| unsafe {
            sys::frida_device_kill_sync(self.device.raw(), pid, ptr::null_mut(), error)
        });
    }
}

impl Drop for Device {
    fn drop(&mut self) {
        let _ = call(|error| unsafe {
            sys::frida_device_manager_close_sync(self.manager.raw(), ptr::null_mut(), error)
        });
    }
}

pub struct Session(Handle<sys::FridaSession>);

impl Session {
    pub fn is_detached(&self) -> bool {
        unsafe { sys::frida_session_is_detached(self.0.raw()) != 0 }
    }

    pub fn load(&self, source: &str, sender: Sender<Result<Value>>) -> Result<Script> {
        let source = CString::new(source)?;
        let options = Handle::new(unsafe { sys::frida_script_options_new() })?;
        unsafe {
            sys::frida_script_options_set_name(options.raw(), c"ck3-local-host".as_ptr());
        }
        let handle = Handle::new(call(|error| unsafe {
            sys::frida_session_create_script_sync(
                self.0.raw(),
                source.as_ptr(),
                options.raw(),
                ptr::null_mut(),
                error,
            )
        })?)?;
        let context = Box::into_raw(Box::new(sender)).cast();
        type Callback = unsafe extern "C" fn(
            *mut sys::FridaScript,
            *const c_char,
            *mut sys::GBytes,
            *mut c_void,
        );
        // GLib holds this box until the message handler is disconnected and any call has finished.
        let signal = unsafe {
            let callback = std::mem::transmute::<Callback, unsafe extern "C" fn()>(on_message);
            sys::g_signal_connect_data(
                handle.raw().cast(),
                c"message".as_ptr(),
                Some(callback),
                context,
                Some(drop_context),
                0,
            )
        };
        let mut script = Script {
            handle,
            signal,
            loaded: false,
        };
        ensure!(signal != 0, "无法监听 Frida 消息");
        call(|error| unsafe {
            sys::frida_script_load_sync(script.handle.raw(), ptr::null_mut(), error)
        })?;
        script.loaded = true;
        Ok(script)
    }
}

impl Drop for Session {
    fn drop(&mut self) {
        if !self.is_detached() {
            let _ = call(|error| unsafe {
                sys::frida_session_detach_sync(self.0.raw(), ptr::null_mut(), error)
            });
        }
    }
}

pub struct Script {
    handle: Handle<sys::FridaScript>,
    signal: sys::gulong,
    loaded: bool,
}

impl Drop for Script {
    fn drop(&mut self) {
        if self.loaded {
            let _ = call(|error| unsafe {
                sys::frida_script_unload_sync(self.handle.raw(), ptr::null_mut(), error)
            });
        }
        if self.signal != 0 {
            unsafe { sys::g_signal_handler_disconnect(self.handle.raw().cast(), self.signal) };
        }
    }
}

unsafe extern "C" fn on_message(
    _: *mut sys::FridaScript,
    message: *const c_char,
    _: *mut sys::GBytes,
    data: *mut c_void,
) {
    if message.is_null() || data.is_null() {
        return;
    }
    let bytes = unsafe { CStr::from_ptr(message) }.to_bytes();
    let parsed: Result<Value> = serde_json::from_slice(bytes).context("解析 CK3 房主适配器消息");
    let event = parsed.and_then(|message| match message["type"].as_str() {
        Some("send") => Ok(message["payload"].clone()),
        Some("error") => Err(anyhow!("{}", message["description"])),
        _ => Ok(Value::Null),
    });
    if matches!(&event, Ok(Value::Null)) {
        return;
    }
    let sender = unsafe { &*data.cast::<Sender<Result<Value>>>() };
    let _ = sender.send(event);
}

unsafe extern "C" fn drop_context(data: *mut c_void, _: *mut sys::GClosure) {
    if !data.is_null() {
        drop(unsafe { Box::from_raw(data.cast::<Sender<Result<Value>>>()) });
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::mpsc;

    #[test]
    fn native_signal_delivers_an_ordinary_script_message() {
        let (send, receive) = mpsc::channel::<Result<Value>>();
        let context = Box::into_raw(Box::new(send)).cast();
        let json =
            c"{\"type\":\"send\",\"payload\":{\"event\":\"local_game_status\",\"connected\":true}}";
        // The test keeps the allocated sender alive through the simulated GLib callback.
        unsafe {
            on_message(ptr::null_mut(), json.as_ptr(), ptr::null_mut(), context);
            drop_context(context, ptr::null_mut());
        }
        let event = receive.recv().unwrap().unwrap();
        assert_eq!(event["event"], "local_game_status");
        assert_eq!(event["connected"], true);
    }
}
