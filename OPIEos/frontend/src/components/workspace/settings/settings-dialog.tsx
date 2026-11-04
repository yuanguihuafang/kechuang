"use client";

import {
  BellIcon,
  CableIcon,
  InfoIcon,
  BrainIcon,
  PaletteIcon,
  PlugZapIcon,
  SparklesIcon,
  UserIcon,
  WrenchIcon,
} from "lucide-react";
import dynamic from "next/dynamic";
import { useEffect, useMemo, useState } from "react";

import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { ScrollArea } from "@/components/ui/scroll-area";
import { useI18n } from "@/core/i18n/hooks";
import { cn } from "@/lib/utils";

function SettingsPageLoading() {
  return (
    <p role="status" className="text-muted-foreground py-8 text-center text-sm">
      Loading…
    </p>
  );
}

const AccountSettingsPage = dynamic(
  () =>
    import("./account-settings-page").then(
      (module) => module.AccountSettingsPage,
    ),
  { loading: SettingsPageLoading },
);
const AppearanceSettingsPage = dynamic(
  () =>
    import("./appearance-settings-page").then(
      (module) => module.AppearanceSettingsPage,
    ),
  { loading: SettingsPageLoading },
);
const ChannelsSettingsPage = dynamic(
  () =>
    import("./channels-settings-page").then(
      (module) => module.ChannelsSettingsPage,
    ),
  { loading: SettingsPageLoading },
);
const IntegrationsSettingsPage = dynamic(
  () =>
    import("./integrations-settings-page").then(
      (module) => module.IntegrationsSettingsPage,
    ),
  { loading: SettingsPageLoading },
);
const MemorySettingsPage = dynamic(
  () =>
    import("./memory-settings-page").then(
      (module) => module.MemorySettingsPage,
    ),
  { loading: SettingsPageLoading },
);
const NotificationSettingsPage = dynamic(
  () =>
    import("./notification-settings-page").then(
      (module) => module.NotificationSettingsPage,
    ),
  { loading: SettingsPageLoading },
);
const SkillSettingsPage = dynamic(
  () =>
    import("./skill-settings-page").then((module) => module.SkillSettingsPage),
  { loading: SettingsPageLoading },
);
const ToolSettingsPage = dynamic(
  () =>
    import("./tool-settings-page").then((module) => module.ToolSettingsPage),
  { loading: SettingsPageLoading },
);
const AboutSettingsPage = dynamic(
  () =>
    import("./about-settings-page").then((module) => module.AboutSettingsPage),
  { loading: SettingsPageLoading },
);

export type SettingsSection =
  | "account"
  | "appearance"
  | "channels"
  | "integrations"
  | "memory"
  | "tools"
  | "skills"
  | "notification"
  | "about";

type SettingsDialogProps = React.ComponentProps<typeof Dialog> & {
  defaultSection?: SettingsSection;
};

export function SettingsDialog(props: SettingsDialogProps) {
  const { defaultSection = "appearance", ...dialogProps } = props;
  const { t } = useI18n();
  const [activeSection, setActiveSection] =
    useState<SettingsSection>(defaultSection);

  useEffect(() => {
    // When opening the dialog, ensure the active section follows the caller's intent.
    // This allows triggers like "About" to open the dialog directly on that page.
    if (dialogProps.open) {
      setActiveSection(defaultSection);
    }
  }, [defaultSection, dialogProps.open]);

  const sections = useMemo(
    () => [
      {
        id: "account",
        label: t.settings.sections.account,
        icon: UserIcon,
      },
      {
        id: "appearance",
        label: t.settings.sections.appearance,
        icon: PaletteIcon,
      },
      {
        id: "notification",
        label: t.settings.sections.notification,
        icon: BellIcon,
      },
      {
        id: "channels",
        label: t.settings.sections.channels,
        icon: CableIcon,
      },
      {
        id: "integrations",
        label: t.settings.sections.integrations,
        icon: PlugZapIcon,
      },
      {
        id: "memory",
        label: t.settings.sections.memory,
        icon: BrainIcon,
      },
      { id: "tools", label: t.settings.sections.tools, icon: WrenchIcon },
      { id: "skills", label: t.settings.sections.skills, icon: SparklesIcon },
      { id: "about", label: t.settings.sections.about, icon: InfoIcon },
    ],
    [
      t.settings.sections.account,
      t.settings.sections.appearance,
      t.settings.sections.channels,
      t.settings.sections.integrations,
      t.settings.sections.memory,
      t.settings.sections.tools,
      t.settings.sections.skills,
      t.settings.sections.notification,
      t.settings.sections.about,
    ],
  );
  return (
    <Dialog
      {...dialogProps}
      onOpenChange={(open) => props.onOpenChange?.(open)}
    >
      <DialogContent
        className="flex h-[75vh] max-h-[calc(100vh-2rem)] flex-col border-0 bg-transparent p-0 shadow-none sm:max-w-5xl md:max-w-6xl"
        aria-describedby={undefined}
      >
        <div className="neumo-raised-lg flex h-full w-full flex-col overflow-hidden rounded-3xl">
          <DialogHeader className="gap-1 border-b border-transparent px-6 pt-6 pb-4">
            <DialogTitle className="text-2xl font-semibold text-[var(--neumo-text)]">
              {t.settings.title}
            </DialogTitle>
            <p className="text-sm text-[var(--neumo-text-muted)]">
              {t.settings.description}
            </p>
          </DialogHeader>
          <div className="grid min-h-0 flex-1 gap-4 px-4 pb-4 md:grid-cols-[220px_minmax(0,1fr)]">
            <nav className="neumo-inset min-h-0 overflow-y-auto rounded-2xl p-2">
              <ul className="space-y-1 pr-1">
                {sections.map(({ id, label, icon: Icon }) => {
                  const active = activeSection === id;
                  return (
                    <li key={id}>
                      <button
                        type="button"
                        onClick={() => setActiveSection(id as SettingsSection)}
                        aria-current={active ? "page" : undefined}
                        className={cn(
                          "flex w-full items-center gap-3 rounded-xl px-3 py-2 text-sm font-medium transition-all duration-300 ease-in-out",
                          active
                            ? "neumo-inset-sm text-[var(--neumo-text)]"
                            : "neumo-btn text-[var(--neumo-text-muted)] hover:text-[var(--neumo-text)]",
                        )}
                      >
                        <Icon
                          className={cn(
                            "size-4 shrink-0 transition-colors",
                            active
                              ? "text-[var(--neumo-accent)]"
                              : "text-[var(--neumo-text-subtle)]",
                          )}
                        />
                        <span>{label}</span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            </nav>
            <ScrollArea className="neumo-scroll neumo-inset h-full min-h-0 rounded-2xl">
              <div className="space-y-8 p-6">
                {activeSection === "account" && <AccountSettingsPage />}
                {activeSection === "appearance" && <AppearanceSettingsPage />}
                {activeSection === "memory" && <MemorySettingsPage />}
                {activeSection === "tools" && <ToolSettingsPage />}
                {activeSection === "skills" && (
                  <SkillSettingsPage
                    onClose={() => props.onOpenChange?.(false)}
                  />
                )}
                {activeSection === "notification" && (
                  <NotificationSettingsPage />
                )}
                {activeSection === "channels" && <ChannelsSettingsPage />}
                {activeSection === "integrations" && (
                  <IntegrationsSettingsPage />
                )}
                {activeSection === "about" && <AboutSettingsPage />}
              </div>
            </ScrollArea>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
