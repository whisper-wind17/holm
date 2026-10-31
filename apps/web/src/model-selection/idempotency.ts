export type ModelWriteAction = "download" | "register";

export interface ModelWriteIntent {
  action: ModelWriteAction;
  repositoryId: string;
  source: "Hugging Face" | "ModelScope" | "Local Path";
}

interface ActiveIntent {
  fingerprint: string;
  key: string;
}

export class ModelWriteIntentKeys {
  private active: ActiveIntent | undefined;
  private readonly randomKey: () => string;

  constructor(randomKey: () => string = () => crypto.randomUUID()) {
    this.randomKey = randomKey;
  }

  acquire(intent: ModelWriteIntent) {
    const fingerprint = JSON.stringify([
      intent.action,
      intent.source,
      intent.repositoryId,
    ]);
    if (this.active?.fingerprint === fingerprint) return this.active.key;
    const key = this.randomKey();
    this.active = { fingerprint, key };
    return key;
  }

  settle(key: string, determinate: boolean) {
    if (determinate && this.active?.key === key) this.active = undefined;
  }

  invalidate() {
    this.active = undefined;
  }
}
