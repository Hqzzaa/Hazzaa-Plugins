import ctypes
import json
import sys
from ctypes import wintypes
from pathlib import Path


def main():
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    procedure = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                                   wintypes.WPARAM, wintypes.LPARAM)
    class WindowClass(ctypes.Structure):
        _fields_ = [("style", wintypes.UINT), ("procedure", procedure),
                    ("class_extra", ctypes.c_int), ("window_extra", ctypes.c_int),
                    ("instance", wintypes.HINSTANCE), ("icon", wintypes.HICON),
                    ("cursor", wintypes.HANDLE), ("background", wintypes.HBRUSH),
                    ("menu", wintypes.LPCWSTR), ("class_name", wintypes.LPCWSTR)]
    user.DefWindowProcW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    user.DefWindowProcW.restype = ctypes.c_ssize_t
    user.CreateWindowExW.argtypes = (wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
        wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p)
    user.CreateWindowExW.restype = wintypes.HWND
    user.DestroyWindow.argtypes = (wintypes.HWND,)
    user.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    user.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
    user.RegisterClassW.argtypes = (ctypes.POINTER(WindowClass),)
    kernel.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
    kernel.GetModuleHandleW.restype = wintypes.HMODULE
    instance = kernel.GetModuleHandleW(None)
    output = Path(sys.argv[1])
    unknown = "--unknown" in sys.argv
    progress_only = '--progress-only' in sys.argv
    bcc_license = '--bcc-license' in sys.argv
    children, fields, result = [], {}, {}

    def create(parent, kind, text, x, y, width, height=24, identity=0, extra_style=0):
        hwnd = user.CreateWindowExW(0, kind, text, 0x50000000 | extra_style,
                                   x, y, width, height, parent, identity, instance, None)
        children.append(hwnd)
        return hwnd

    def create_registration_fields(parent):
        for name, label, y, style in (("username", "Username:", 20, 0),
                                      ("password", "Password:", 65, 0x20)):
            create(parent, "STATIC", label, 20, y, 120)
            fields[name] = create(parent, "EDIT", "", 160, y, 270, identity=101 + y, extra_style=style)
        create(parent, "STATIC", "Serial number:", 20, 110, 120)
        for index in range(3):
            fields[f"serial{index}"] = create(parent, "EDIT", "", 160 + index * 90, 110, 80, identity=200 + index)
        create(parent, "STATIC", "Destination:", 20, 155, 120)
        fields["destination"] = create(parent, "EDIT", "C:\\Example", 160, 155, 270, identity=250)
        if unknown:
            create(parent, "STATIC", "Project ID:", 20, 200, 120)
            fields["unknown"] = create(parent, "EDIT", "", 160, 200, 270, identity=251)
        create(parent, "BUTTON", "Next >", 340, 260, 100, identity=301)

    @procedure
    def on_message(hwnd, message, wparam, lparam):
        if message == 0x0111:
            button = wparam & 0xFFFF
            if button == 301:
                if bcc_license:
                    user.SendMessageW.argtypes=(wintypes.HWND,wintypes.UINT,wintypes.WPARAM,wintypes.LPARAM)
                    result['without_license']=user.SendMessageW(fields['without_license'],0xF0,0,0)==1
                    result['next_clicked']=True
                    output.write_text(json.dumps(result),encoding='utf-8')
                    user.DestroyWindow(hwnd);return 0
                for name, control in fields.items():
                    buffer = ctypes.create_unicode_buffer(1024)
                    user.GetWindowTextW(control, buffer, len(buffer))
                    result[name] = buffer.value
                result["submitted"] = True
                output.write_text(json.dumps(result), encoding="utf-8")
                for child in children[:]:
                    user.DestroyWindow(child)
                children.clear()
                create(hwnd, "STATIC", "Ready to install", 20, 20, 300)
                create(hwnd, "BUTTON", "Install", 340, 260, 100, identity=302)
                return 0
            if button == 302:
                result["installed"] = True
                output.write_text(json.dumps(result), encoding="utf-8")
                user.DestroyWindow(hwnd)
                return 0
        if message == 0x0113 and progress_only:
            output.write_text(json.dumps({'installed':True}),encoding='utf-8')
            user.DestroyWindow(hwnd);return 0
        if message == 0x0002:
            user.PostQuitMessage(0)
            return 0
        return user.DefWindowProcW(hwnd, message, wparam, lparam)

    cls = WindowClass(0, on_message, 0, 0, instance, None, None, 6, None, "HazzaaNativeInstallerTest")
    if not user.RegisterClassW(ctypes.byref(cls)):
        raise ctypes.WinError(ctypes.get_last_error())
    title='Boris FX License Tool' if bcc_license else 'HAZZAA LOCAL INSTALLER TEST'
    window = user.CreateWindowExW(0, cls.class_name, title, 0x00CF0000,
                                 80, 80, 520, 360, None, None, instance, None)
    if not window:
        raise ctypes.WinError(ctypes.get_last_error())
    if progress_only:
        ctypes.WinDLL('comctl32').InitCommonControls()
        create(window,'STATIC','Installing files...',20,20,300)
        bar=create(window,'msctls_progress32','',20,80,420)
        if not bar:raise ctypes.WinError(ctypes.get_last_error())
        create(window,'BUTTON','Cancel',340,260,100,identity=399)
        user.SetTimer.argtypes=(wintypes.HWND,ctypes.c_size_t,wintypes.UINT,ctypes.c_void_p)
        user.SetTimer(window,1,900,None)
    elif bcc_license:
        create(window,'STATIC','Not licensed',20,20,300)
        create(window,'STATIC','Continuum is not licensed.',20,50,350)
        fields['without_license']=create(window,'BUTTON','Complete installation without entering a license',20,100,450,identity=290,extra_style=9)
        create(window,'BUTTON','Next >',340,260,100,identity=301)
    else:
        create_registration_fields(window)
    user.ShowWindow(window,4)
    message=wintypes.MSG()
    while user.GetMessageW(ctypes.byref(message),None,0,0)>0:
        user.TranslateMessage(ctypes.byref(message));user.DispatchMessageW(ctypes.byref(message))


if __name__ == "__main__":
    main()
