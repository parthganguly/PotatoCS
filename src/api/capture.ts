import { invoke } from "@tauri-apps/api/core";
import { CaptureCapabilities, CaptureResult } from "../tauri";

export async function captureCapabilities(): Promise<CaptureCapabilities> {
  return invoke<CaptureCapabilities>("capture_capabilities");
}

export async function importClipboardImage(): Promise<CaptureResult> {
  return invoke<CaptureResult>("clipboard_import_image");
}

export async function captureFullScreen(screenIndex?: number): Promise<CaptureResult> {
  return invoke<CaptureResult>("capture_full_screen", { screenIndex });
}

export async function captureRegion(params: {
  x: number;
  y: number;
  width: number;
  height: number;
  screenIndex?: number;
}): Promise<CaptureResult> {
  return invoke<CaptureResult>("capture_region", {
    x: params.x,
    y: params.y,
    width: params.width,
    height: params.height,
    screenIndex: params.screenIndex
  });
}

export async function captureWindow(): Promise<CaptureResult> {
  return invoke<CaptureResult>("capture_window");
}
