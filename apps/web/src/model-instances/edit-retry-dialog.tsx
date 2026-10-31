import { useState } from "react";
import { useNavigate } from "react-router";

import type { ModelInstance } from "../api/model-instance-contracts";
import { Button, InlineAlert, Modal, type ButtonVariant } from "../components";
import { instanceFailurePresentation, runtimeLabel } from "./presentation";

/**
 * "修改配置并重试" first explains what happens, then hands over
 * to the deploy wizard. The wizard itself owns step skipping and the
 * locked model; this dialog only navigates with the source instance ID.
 */
export function EditRetryDialog({
  gpuText,
  instance,
  modelName,
  variant = "secondary",
}: {
  gpuText: string;
  instance: ModelInstance;
  modelName: string;
  variant?: ButtonVariant;
}) {
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const failure = instanceFailurePresentation(instance.error_code);
  return (
    <Modal
      footer={
        <>
          <Button
            onClick={() => {
              setOpen(false);
            }}
          >
            取消
          </Button>
          <Button
            onClick={() => {
              setOpen(false);
              void navigate(
                `/models/deploy?retry_instance_id=${encodeURIComponent(instance.id)}`,
              );
            }}
            variant="primary"
          >
            前往部署向导
          </Button>
        </>
      }
      onOpenChange={setOpen}
      open={open}
      title={`修改配置并重试「${instance.name}」`}
      trigger={
        <Button size="s" variant={variant}>
          修改配置并重试
        </Button>
      }
    >
      <InlineAlert title="将打开部署向导" tone="info">
        带入上次的配置，可以修改
        GPU、Runtime、上下文长度、显存比例和实例名称；模型不能换。确认后新建一个模型实例，「
        {instance.name}
        」保留为运行失败，不会被重试或删除，可在新实例运行后删除。
      </InlineAlert>
      <dl className="pw-instance-detail__facts">
        <RetryFact label="模型" value={modelName} />
        <RetryFact
          label="Runtime"
          value={runtimeLabel(instance.runtime_type)}
        />
        <RetryFact label="GPU" value={gpuText} />
        <RetryFact label="上次失败原因" value={failure.reason} />
        <RetryFact label="建议" value={failure.advice} />
      </dl>
    </Modal>
  );
}

function RetryFact({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}
