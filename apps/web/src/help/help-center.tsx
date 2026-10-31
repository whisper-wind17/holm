import * as Dialog from "@radix-ui/react-dialog";
import {
  createContext,
  useContext,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { Button } from "../components";
import { HELP_TOPICS, HelpBody, helpBody, type HelpTopicId } from "./content";

const HelpContext = createContext<
  ((topic: string | undefined, trigger: HTMLElement) => void) | undefined
>(undefined);

export function HelpButton({
  topic,
  children = "帮助",
}: {
  topic?: HelpTopicId;
  children?: ReactNode;
}) {
  const open = useContext(HelpContext);
  // These pages can also be rendered in isolation outside the authenticated shell.
  if (!open) return null;
  return (
    <Button
      icon="help"
      size="s"
      variant="ghost"
      onClick={(event) => {
        open(topic, event.currentTarget);
      }}
    >
      {children}
    </Button>
  );
}

export function HelpProvider({
  admin,
  children,
}: {
  admin: boolean;
  children: ReactNode;
}) {
  const [selected, setSelected] = useState<string>();
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLElement>(null);
  const title = useRef<HTMLHeadingElement>(null);
  const topics = Object.values(HELP_TOPICS).filter(
    (topic) => admin || !topic.adminOnly,
  );
  const body = selected === undefined ? undefined : helpBody(selected, admin);
  const topic = topics.find((item) => item.id === selected);

  return (
    <HelpContext.Provider
      value={(id, element) => {
        trigger.current = element;
        setSelected(id);
        setOpen(true);
      }}
    >
      {children}
      <Dialog.Root open={open} onOpenChange={setOpen}>
        <Dialog.Portal>
          <Dialog.Overlay className="pw-dialog-overlay" />
          <Dialog.Content
            className="pw-dialog pw-dialog--xl pw-help"
            onOpenAutoFocus={(event) => {
              event.preventDefault();
              title.current?.focus();
            }}
            onCloseAutoFocus={(event) => {
              event.preventDefault();
              trigger.current?.focus();
            }}
          >
            <header className="pw-dialog__header">
              <div>
                <Dialog.Title asChild>
                  <h2 tabIndex={-1} ref={title}>
                    帮助中心
                  </h2>
                </Dialog.Title>
                <Dialog.Description>
                  按当前任务阅读指引，关闭后继续操作。
                </Dialog.Description>
              </div>
              <Dialog.Close asChild>
                <Button
                  icon="close"
                  size="s"
                  variant="ghost"
                  aria-label="关闭帮助"
                >
                  关闭
                </Button>
              </Dialog.Close>
            </header>
            <div className="pw-dialog__body">
              <nav className="pw-help__topics" aria-label="帮助主题">
                {topics.map((item) => (
                  <Button
                    key={item.id}
                    size="s"
                    variant="ghost"
                    selected={selected === item.id}
                    onClick={() => {
                      setSelected(item.id);
                    }}
                  >
                    {item.title}
                  </Button>
                ))}
              </nav>
              <article
                className="pw-help__body"
                aria-label={topic?.title ?? "帮助目录"}
              >
                {selected === undefined ? (
                  <>
                    <h2>选择你正在处理的任务</h2>
                    <p>
                      从上方主题查看操作顺序、注意事项和常见问题。帮助随当前前端版本提供，无需访问外网。
                    </p>
                  </>
                ) : body === undefined ? (
                  <>
                    <h2>无法打开此帮助主题</h2>
                    <p>
                      主题不存在或当前账号无权查看。请选择上方可用主题，或返回帮助目录。
                    </p>
                    <Button
                      onClick={() => {
                        setSelected(undefined);
                      }}
                    >
                      返回帮助目录
                    </Button>
                  </>
                ) : (
                  <>
                    <h2>{topic?.title}</h2>
                    <HelpBody text={body} />
                  </>
                )}
              </article>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </HelpContext.Provider>
  );
}
