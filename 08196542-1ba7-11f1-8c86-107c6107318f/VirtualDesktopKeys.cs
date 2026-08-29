using System;
using System.Runtime.InteropServices;

public static class VirtualDesktopKeys
{
    [DllImport("user32.dll", SetLastError = true)]
    public static extern void keybd_event(byte vk, byte scan, uint flags, UIntPtr extra);

    public const uint KEYUP = 2;
    public const byte WIN = 0x5b;
    public const byte CTRL = 0x11;
    public const byte RIGHT = 0x27;
    public const byte LEFT = 0x25;

    public static void Press(byte key)
    {
        keybd_event(WIN, 0, 0, UIntPtr.Zero);
        keybd_event(CTRL, 0, 0, UIntPtr.Zero);
        keybd_event(key, 0, 0, UIntPtr.Zero);
        keybd_event(key, 0, KEYUP, UIntPtr.Zero);
        keybd_event(CTRL, 0, KEYUP, UIntPtr.Zero);
        keybd_event(WIN, 0, KEYUP, UIntPtr.Zero);
    }
}
