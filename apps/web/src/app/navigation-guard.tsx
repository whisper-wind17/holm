import * as Dialog from "@radix-ui/react-dialog";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useLocation, useNavigate } from "react-router";

import { Button } from "../components";

/**
 * What a page with an unsaved draft tells the guard. `discard` runs when the
 * user confirms leaving, so the draft does not outlive the decision (a tab that
 * stays mounted would otherwise keep it).
 */
export interface UnsavedChanges {
  /** Shown as the confirm dialog title, e.g. "放弃未保存的访问权限更改？". */
  title: string;
  message: string;
  discard: () => void;
}

type EntryGetter = () => UnsavedChanges | null;

interface NavigationGuard {
  confirmLeave: (proceed: () => void) => void;
  register: (id: string, getter: EntryGetter) => () => void;
}

const NavigationGuardContext = createContext<NavigationGuard | null>(null);

function dirtyEntries(getters: ReadonlyMap<string, EntryGetter>) {
  return [...getters.values()].flatMap((getter) => {
    const entry = getter();
    return entry === null ? [] : [entry];
  });
}

/**
 * A lightweight in-app navigation guard. The app uses BrowserRouter, so
 * `useBlocker` is unavailable. While a page reports unsaved changes, clicks on
 * same-origin links are intercepted here (one capture listener covers the
 * sidebar, breadcrumb, user menu and in-page links alike) and the user is asked
 * first; page code asks through `useConfirmLeave` for
 * navigation that is not a link, such as switching tabs.
 * Browser back/forward is not intercepted; closing or reloading is covered by
 * `beforeunload` in `useUnsavedChangesGuard`.
 */
export function NavigationGuardProvider({ children }: { children: ReactNode }) {
  const navigate = useNavigate();
  const location = useLocation();
  const getters = useRef(new Map<string, EntryGetter>());
  const [pending, setPending] = useState<{
    entries: UnsavedChanges[];
    proceed: () => void;
  }>();
  const here = useRef(`${location.pathname}${location.search}${location.hash}`);
  here.current = `${location.pathname}${location.search}${location.hash}`;

  const confirmLeave = useCallback((proceed: () => void) => {
    const entries = dirtyEntries(getters.current);
    if (entries.length === 0) proceed();
    else setPending({ entries, proceed });
  }, []);

  const register = useCallback((id: string, getter: EntryGetter) => {
    getters.current.set(id, getter);
    return () => {
      getters.current.delete(id);
    };
  }, []);

  useEffect(() => {
    function onClick(event: MouseEvent) {
      if (
        event.defaultPrevented ||
        event.button !== 0 ||
        event.metaKey ||
        event.ctrlKey ||
        event.shiftKey ||
        event.altKey ||
        !(event.target instanceof Element)
      )
        return;
      const anchor = event.target.closest("a[href]");
      if (!(anchor instanceof HTMLAnchorElement)) return;
      if (
        (anchor.target !== "" && anchor.target !== "_self") ||
        anchor.hasAttribute("download") ||
        anchor.getAttribute("href")?.startsWith("#") === true
      )
        return;
      const url = new URL(anchor.href, window.location.href);
      if (url.origin !== window.location.origin) return;
      const destination = `${url.pathname}${url.search}${url.hash}`;
      if (destination === here.current) return;
      if (dirtyEntries(getters.current).length === 0) return;
      event.preventDefault();
      event.stopPropagation();
      confirmLeave(() => {
        void navigate(destination);
      });
    }
    document.addEventListener("click", onClick, true);
    return () => {
      document.removeEventListener("click", onClick, true);
    };
  }, [confirmLeave, navigate]);

  const value = useMemo(
    () => ({ confirmLeave, register }),
    [confirmLeave, register],
  );
  const first = pending?.entries[0];

  return (
    <NavigationGuardContext.Provider value={value}>
      {children}
      <Dialog.Root
        onOpenChange={(open) => {
          if (!open) setPending(undefined);
        }}
        open={pending !== undefined}
      >
        <Dialog.Portal>
          <Dialog.Overlay className="pw-dialog-overlay" />
          <Dialog.Content
            aria-describedby="pw-leave-message"
            className="pw-dialog pw-dialog--s"
          >
            <header className="pw-dialog__header">
              <div>
                <Dialog.Title asChild>
                  <h2>{first?.title ?? "放弃未保存的更改？"}</h2>
                </Dialog.Title>
              </div>
            </header>
            <div className="pw-dialog__body">
              <p id="pw-leave-message">{first?.message}</p>
            </div>
            <footer className="pw-dialog__footer">
              <Button
                onClick={() => {
                  setPending(undefined);
                }}
              >
                继续编辑
              </Button>
              <Button
                onClick={() => {
                  const current = pending;
                  setPending(undefined);
                  if (current === undefined) return;
                  current.entries.forEach((entry) => {
                    entry.discard();
                  });
                  current.proceed();
                }}
                variant="danger"
              >
                放弃更改
              </Button>
            </footer>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </NavigationGuardContext.Provider>
  );
}

/**
 * Run `proceed` at once when nothing is dirty, otherwise after the user agrees
 * to discard. Without a provider (isolated renders) it never blocks.
 */
export function useConfirmLeave() {
  const guard = useContext(NavigationGuardContext);
  return useCallback(
    (proceed: () => void) => {
      if (guard === null) proceed();
      else guard.confirmLeave(proceed);
    },
    [guard],
  );
}

/**
 * Report unsaved changes (`null` when clean) and, while dirty, ask the browser
 * to confirm closing or reloading the page.
 */
export function useUnsavedChangesGuard(changes: UnsavedChanges | null) {
  const guard = useContext(NavigationGuardContext);
  const id = useId();
  const latest = useRef(changes);
  useEffect(() => {
    latest.current = changes;
  });
  const register = guard?.register;
  useEffect(() => {
    return register?.(id, () => latest.current);
  }, [id, register]);
  const dirty = changes !== null;
  useEffect(() => {
    if (!dirty) return;
    function onBeforeUnload(event: BeforeUnloadEvent) {
      event.preventDefault();
    }
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => {
      window.removeEventListener("beforeunload", onBeforeUnload);
    };
  }, [dirty]);
}
