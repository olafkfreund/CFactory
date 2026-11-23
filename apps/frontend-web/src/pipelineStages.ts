import type { WorkItem } from "./api";
import { stageState } from "./taskState";

export type StripStage = "pfactory" | "aifactory" | "tfactory";

export interface StripAlarm {
  stage: StripStage;
  tick: number;
}

export function stageFailed(wi: WorkItem, stage: StripStage): boolean {
  return stageState(wi[stage].status) === "failed";
}

export function hasFailedStage(wi: WorkItem, stage?: StripStage | null): boolean {
  if (stage) return stageFailed(wi, stage);
  return stageFailed(wi, "pfactory") || stageFailed(wi, "aifactory") || stageFailed(wi, "tfactory");
}
