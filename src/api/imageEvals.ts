import { ImageEvalRun, ImageEvalSuite, MultimodalMode, rpc } from "../tauri";

export async function listImageEvalSuite(): Promise<ImageEvalSuite> {
  return rpc<ImageEvalSuite>("image_evals.list");
}

export async function runImageEval(mode: MultimodalMode, model = ""): Promise<ImageEvalRun> {
  return rpc<ImageEvalRun>("image_evals.run", { mode, model });
}

export async function listImageEvalHistory(limit = 20): Promise<ImageEvalRun[]> {
  return rpc<ImageEvalRun[]>("image_evals.history", { limit });
}
