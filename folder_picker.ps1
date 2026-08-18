$ErrorActionPreference = 'Stop'
$source = @'
using System;
using System.Runtime.InteropServices;

[Flags]
internal enum FOS : uint {
    PICKFOLDERS = 0x00000020,
    FORCEFILESYSTEM = 0x00000040,
    PATHMUSTEXIST = 0x00000800,
    DONTADDTORECENT = 0x02000000
}
internal enum SIGDN : uint { FILESYSTEMPATH = 0x80058000 }

[ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown), Guid("b4db1657-70d7-485e-8e3e-6fcb5a5c1802")]
internal interface IModalWindow { [PreserveSig] int Show(IntPtr parent); }

[ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown), Guid("d57c7288-d4ad-4768-be02-9d969532d960")]
internal interface IFileOpenDialog : IModalWindow {
    [PreserveSig] new int Show(IntPtr parent);
    [PreserveSig] int SetFileTypes(uint count, IntPtr specs);
    [PreserveSig] int SetFileTypeIndex(uint index);
    [PreserveSig] int GetFileTypeIndex(out uint index);
    [PreserveSig] int Advise(IntPtr events, out uint cookie);
    [PreserveSig] int Unadvise(uint cookie);
    [PreserveSig] int SetOptions(FOS options);
    [PreserveSig] int GetOptions(out FOS options);
    [PreserveSig] int SetDefaultFolder(IShellItem item);
    [PreserveSig] int SetFolder(IShellItem item);
    [PreserveSig] int GetFolder(out IShellItem item);
    [PreserveSig] int GetCurrentSelection(out IShellItem item);
    [PreserveSig] int SetFileName([MarshalAs(UnmanagedType.LPWStr)] string name);
    [PreserveSig] int GetFileName([MarshalAs(UnmanagedType.LPWStr)] out string name);
    [PreserveSig] int SetTitle([MarshalAs(UnmanagedType.LPWStr)] string title);
    [PreserveSig] int SetOkButtonLabel([MarshalAs(UnmanagedType.LPWStr)] string text);
    [PreserveSig] int SetFileNameLabel([MarshalAs(UnmanagedType.LPWStr)] string text);
    [PreserveSig] int GetResult(out IShellItem item);
    [PreserveSig] int AddPlace(IShellItem item, int placement);
    [PreserveSig] int SetDefaultExtension([MarshalAs(UnmanagedType.LPWStr)] string extension);
    [PreserveSig] int Close(int hr);
    [PreserveSig] int SetClientGuid(ref Guid guid);
    [PreserveSig] int ClearClientData();
    [PreserveSig] int SetFilter(IntPtr filter);
    [PreserveSig] int GetResults(out IntPtr items);
    [PreserveSig] int GetSelectedItems(out IntPtr items);
}

[ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown), Guid("43826d1e-e718-42ee-bc55-a1e261c37bfe")]
internal interface IShellItem {
    [PreserveSig] int BindToHandler(IntPtr pbc, ref Guid bhid, ref Guid riid, out IntPtr ppv);
    [PreserveSig] int GetParent(out IShellItem parent);
    [PreserveSig] int GetDisplayName(SIGDN sigdnName, out IntPtr ppszName);
    [PreserveSig] int GetAttributes(uint mask, out uint attributes);
    [PreserveSig] int Compare(IShellItem other, uint hint, out int order);
}

[ComImport, Guid("DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7")]
internal class FileOpenDialog { }

public static class FolderPicker {
    public static string SelectFolder() {
        IFileOpenDialog dialog = (IFileOpenDialog)new FileOpenDialog();
        try {
            FOS options;
            dialog.GetOptions(out options);
            dialog.SetOptions(options | FOS.PICKFOLDERS | FOS.FORCEFILESYSTEM | FOS.PATHMUSTEXIST | FOS.DONTADDTORECENT);
            int result = dialog.Show(IntPtr.Zero);
            if (result != 0) return "";
            IShellItem item;
            dialog.GetResult(out item);
            IntPtr path;
            item.GetDisplayName(SIGDN.FILESYSTEMPATH, out path);
            try { return Marshal.PtrToStringUni(path) ?? ""; }
            finally { Marshal.FreeCoTaskMem(path); }
        } finally {
            Marshal.FinalReleaseComObject(dialog);
        }
    }
}
'@
Add-Type -TypeDefinition $source
$selected = [FolderPicker]::SelectFolder()
[Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($selected))
