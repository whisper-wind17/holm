import * as RadixTabs from "@radix-ui/react-tabs";
import type { ReactNode } from "react";

export interface TabItem {
  content: ReactNode;
  disabled?: boolean;
  label: string;
  value: string;
}

export interface TabsProps {
  "aria-label": string;
  defaultValue: string;
  items: readonly TabItem[];
  onValueChange?: (value: string) => void;
}

export function Tabs({
  "aria-label": ariaLabel,
  defaultValue,
  items,
  onValueChange,
}: TabsProps) {
  return (
    <RadixTabs.Root
      className="pw-tabs"
      defaultValue={defaultValue}
      onValueChange={onValueChange}
      orientation="horizontal"
    >
      <RadixTabs.List aria-label={ariaLabel} className="pw-tabs__list">
        {items.map((item) => (
          <RadixTabs.Trigger
            className="pw-tabs__trigger"
            disabled={item.disabled}
            key={item.value}
            value={item.value}
          >
            {item.label}
          </RadixTabs.Trigger>
        ))}
      </RadixTabs.List>
      {items.map((item) => (
        <RadixTabs.Content
          className="pw-tabs__content"
          key={item.value}
          value={item.value}
        >
          {item.content}
        </RadixTabs.Content>
      ))}
    </RadixTabs.Root>
  );
}
