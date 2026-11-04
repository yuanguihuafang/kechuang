"use client";

import {
  ArrowLeftIcon,
  BotIcon,
  CheckCircleIcon,
  InfoIcon,
  MoreHorizontalIcon,
  SaveIcon,
} from "lucide-react";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import { toast } from "sonner";

import {
  PromptInput,
  PromptInputFooter,
  PromptInputSubmit,
  PromptInputTextarea,
} from "@/components/ai-elements/prompt-input";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { ArtifactsProvider } from "@/components/workspace/artifacts";
import { MessageList } from "@/components/workspace/messages";
import { ThreadContext } from "@/components/workspace/messages/context";
import type { Agent } from "@/core/agents";
import {
  AgentNameCheckError,
  AgentsApiDisabledError,
  checkAgentName,
  getAgent,
} from "@/core/agents/api";
import { useI18n } from "@/core/i18n/hooks";
import {
  buildHumanInputResponseText,
  type HumanInputRequest,
  type HumanInputResponse,
} from "@/core/messages/human-input";
import { safeLocalStorage } from "@/core/settings/local";
import { hasToolResult, useThreadStream } from "@/core/threads/hooks";
import { uuid } from "@/core/utils/uuid";
import { isIMEComposing } from "@/lib/ime";
import { cn } from "@/lib/utils";

type Step = "name" | "chat";
type SetupAgentStatus = "idle" | "requested" | "completed";

const NAME_RE = /^[A-Za-z0-9-]+$/;
const SAVE_HINT_STORAGE_KEY = "opie-os.agent-create.save-hint-seen:v1";
const AGENT_READ_RETRY_DELAYS_MS = [200, 500, 1_000, 2_000];

function wait(ms: number) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

async function getAgentWithRetry(agentName: string) {
  for (const delay of [0, ...AGENT_READ_RETRY_DELAYS_MS]) {
    if (delay > 0) {
      await wait(delay);
    }

    try {
      return await getAgent(agentName);
    } catch {
      // Retry until the write settles or the attempts are exhausted.
    }
  }

  return null;
}

export default function NewAgentPage() {
  const { t } = useI18n();
  const router = useRouter();

  const [step, setStep] = useState<Step>("name");
  const [nameInput, setNameInput] = useState("");
  const [nameError, setNameError] = useState("");
  const [isCheckingName, setIsCheckingName] = useState(false);
  const [agentName, setAgentName] = useState("");
  const [agent, setAgent] = useState<Agent | null>(null);
  const [showSaveHint, setShowSaveHint] = useState(false);
  const [setupAgentStatus, setSetupAgentStatus] =
    useState<SetupAgentStatus>("idle");

  const threadId = useMemo(() => uuid(), []);

  const { thread, sendMessage } = useThreadStream({
    threadId: undefined,
    context: {
      mode: "flash",
      is_bootstrap: true,
    },
    onFinish(state) {
      if (agent || setupAgentStatus !== "requested") {
        return;
      }
      if (!agentName || !hasToolResult(state.messages, "setup_agent")) {
        setSetupAgentStatus("idle");
        return;
      }
      setSetupAgentStatus("completed");
      void getAgentWithRetry(agentName).then((fetched) => {
        if (fetched) {
          setAgent(fetched);
          return;
        }

        toast.error(t.agents.agentCreatedPendingRefresh);
      });
    },
  });
  useEffect(() => {
    if (typeof window === "undefined" || step !== "chat") {
      return;
    }
    if (safeLocalStorage.getItem(SAVE_HINT_STORAGE_KEY) === "1") {
      return;
    }
    setShowSaveHint(true);
    safeLocalStorage.setItem(SAVE_HINT_STORAGE_KEY, "1");
  }, [step]);

  const handleConfirmName = useCallback(async () => {
    const trimmed = nameInput.trim();
    if (!trimmed) return;
    if (!NAME_RE.test(trimmed)) {
      setNameError(t.agents.nameStepInvalidError);
      return;
    }

    setNameError("");
    setIsCheckingName(true);
    try {
      const result = await checkAgentName(trimmed);
      if (!result.available) {
        setNameError(t.agents.nameStepAlreadyExistsError);
        return;
      }
    } catch (err) {
      if (err instanceof AgentsApiDisabledError) {
        setNameError(t.agents.nameStepApiDisabledError);
      } else if (
        err instanceof AgentNameCheckError &&
        err.reason === "backend_unreachable"
      ) {
        setNameError(t.agents.nameStepNetworkError);
      } else if (
        err instanceof AgentNameCheckError &&
        err.reason === "request_failed"
      ) {
        // Surface the backend-provided detail (e.g. validation error) when
        // one is present, wrapped in a localised prefix so zh-CN users
        // don't see a bare English string next to the surrounding Chinese
        // UI. Falls back to the generic localised fallback when the backend
        // sent no detail — `err.message` is unreliable for this branch
        // because `checkAgentName` substitutes a generated fallback string
        // ("Failed to check agent name: ${statusText}") when `detail` is
        // missing, so testing `err.message` would always be truthy and the
        // generated fallback would leak through.
        setNameError(
          err.detail
            ? t.agents.nameStepCheckErrorWithDetail.replace(
                "{detail}",
                err.detail,
              )
            : t.agents.nameStepCheckError,
        );
      } else {
        setNameError(t.agents.nameStepCheckError);
      }
      return;
    } finally {
      setIsCheckingName(false);
    }

    setAgentName(trimmed);
    setStep("chat");
    await sendMessage(
      threadId,
      {
        text: t.agents.nameStepBootstrapMessage.replace("{name}", trimmed),
        files: [],
      },
      { agent_name: trimmed },
    );
  }, [
    nameInput,
    sendMessage,
    t.agents.nameStepAlreadyExistsError,
    t.agents.nameStepApiDisabledError,
    t.agents.nameStepNetworkError,
    t.agents.nameStepBootstrapMessage,
    t.agents.nameStepCheckError,
    t.agents.nameStepCheckErrorWithDetail,
    t.agents.nameStepInvalidError,
    threadId,
  ]);

  const handleNameKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter" && !isIMEComposing(e)) {
      e.preventDefault();
      void handleConfirmName();
    }
  };

  const handleChatSubmit = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || thread.isLoading) return;
      await sendMessage(
        threadId,
        { text: trimmed, files: [] },
        { agent_name: agentName },
      );
    },
    [agentName, sendMessage, thread.isLoading, threadId],
  );

  const handleSubmitHumanInput = useCallback(
    async (request: HumanInputRequest, response: HumanInputResponse) => {
      if (!agentName) {
        return false;
      }

      let sent = false;
      await sendMessage(
        threadId,
        {
          text: buildHumanInputResponseText(request, response),
          files: [],
        },
        { agent_name: agentName },
        {
          additionalKwargs: {
            hide_from_ui: true,
            human_input_response: response,
          },
          onSent: () => {
            sent = true;
          },
        },
      );
      return sent;
    },
    [agentName, sendMessage, threadId],
  );

  const handleSaveAgent = useCallback(async () => {
    if (
      !agentName ||
      agent ||
      thread.isLoading ||
      setupAgentStatus !== "idle"
    ) {
      return;
    }

    setSetupAgentStatus("requested");
    setShowSaveHint(false);
    try {
      await sendMessage(
        threadId,
        { text: t.agents.saveCommandMessage, files: [] },
        { agent_name: agentName },
        { additionalKwargs: { hide_from_ui: true } },
      );
      toast.success(t.agents.saveRequested);
    } catch (error) {
      setSetupAgentStatus("idle");
      toast.error(error instanceof Error ? error.message : String(error));
    }
  }, [
    agent,
    agentName,
    sendMessage,
    setupAgentStatus,
    t.agents.saveCommandMessage,
    t.agents.saveRequested,
    thread.isLoading,
    threadId,
  ]);

  const header = (
    <header className="flex shrink-0 items-center justify-between gap-3 px-4 py-3">
      <div className="flex items-center gap-3">
        <Button
          variant="ghost"
          size="icon-sm"
          className="neumo-btn size-8 rounded-xl p-0 text-[color:var(--neumo-text)]"
          onClick={() => router.push("/workspace/agents")}
        >
          <ArrowLeftIcon className="h-4 w-4" />
        </Button>
        <h1 className="text-sm font-semibold text-[color:var(--neumo-text)]">
          {t.agents.createPageTitle}
        </h1>
      </div>

      {step === "chat" ? (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              variant="ghost"
              size="icon-sm"
              className="neumo-btn size-8 rounded-xl p-0 text-[color:var(--neumo-text)]"
              aria-label={t.agents.more}
            >
              <MoreHorizontalIcon className="h-4 w-4" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent
            align="end"
            className="neumo-raised rounded-2xl p-2"
          >
            <DropdownMenuItem
              className="rounded-xl px-3 py-2 text-sm text-[color:var(--neumo-text)] focus:bg-[color:var(--neumo-bg-soft)] focus:text-[color:var(--neumo-text)]"
              onSelect={() => void handleSaveAgent()}
              disabled={[
                Boolean(agent),
                thread.isLoading,
                setupAgentStatus !== "idle",
              ].some(Boolean)}
            >
              <SaveIcon className="h-4 w-4" />
              {setupAgentStatus === "requested"
                ? t.agents.saving
                : t.agents.save}
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      ) : null}
    </header>
  );

  if (step === "name") {
    return (
      <div className="flex size-full flex-col">
        {header}
        <main className="flex flex-1 flex-col items-center justify-center px-4">
          <div className="w-full max-w-sm space-y-8">
            <div className="space-y-3 text-center">
              <div className="neumo-raised mx-auto inline-flex h-16 w-16 items-center justify-center rounded-3xl text-[color:var(--neumo-accent-text)]">
                <BotIcon className="h-8 w-8" />
              </div>
              <div className="space-y-1">
                <h2 className="text-xl font-semibold text-[color:var(--neumo-text)]">
                  {t.agents.nameStepTitle}
                </h2>
                <p className="text-sm text-[color:var(--neumo-text-muted)]">
                  {t.agents.nameStepHint}
                </p>
              </div>
            </div>

            <div className="space-y-3">
              <Input
                autoFocus
                placeholder={t.agents.nameStepPlaceholder}
                value={nameInput}
                onChange={(e) => {
                  setNameInput(e.target.value);
                  setNameError("");
                }}
                onKeyDown={handleNameKeyDown}
                className={cn(
                  "neumo-input rounded-2xl px-4 py-3 text-sm",
                  nameError && "text-[color:var(--neumo-danger)]",
                )}
              />
              {nameError ? (
                <div className="neumo-inset-sm rounded-xl px-4 py-2.5 text-sm text-[color:var(--neumo-danger)]">
                  {nameError}
                </div>
              ) : null}
              <Button
                className="neumo-btn-accent mt-2 h-12 w-full rounded-2xl text-sm font-medium"
                onClick={() => void handleConfirmName()}
                disabled={!nameInput.trim() || isCheckingName}
              >
                {t.agents.nameStepContinue}
              </Button>
            </div>
          </div>
        </main>
      </div>
    );
  }

  return (
    <ThreadContext.Provider value={{ thread }}>
      <ArtifactsProvider>
        <div className="flex size-full flex-col">
          {header}

          <main className="flex min-h-0 flex-1 flex-col">
            {showSaveHint ? (
              <div className="px-4 pt-4">
                <div className="mx-auto w-full max-w-(--container-width-md)">
                  <div className="neumo-raised-sm flex items-start gap-3 rounded-2xl px-4 py-3 text-sm text-[color:var(--neumo-text-muted)]">
                    <span className="neumo-inset-sm mt-0.5 inline-flex size-7 items-center justify-center rounded-xl text-[color:var(--neumo-info)]">
                      <InfoIcon className="h-4 w-4" />
                    </span>
                    <span className="text-[color:var(--neumo-text)]">
                      {t.agents.saveHint}
                    </span>
                  </div>
                </div>
              </div>
            ) : null}

            <div className="flex min-h-0 flex-1 justify-center">
              <MessageList
                className={cn("size-full", showSaveHint ? "pt-4" : "pt-10")}
                threadId={threadId}
                thread={thread}
                onSubmitHumanInput={
                  agentName ? handleSubmitHumanInput : undefined
                }
              />
            </div>

            <div className="neumo-surface flex shrink-0 justify-center px-4 py-4">
              <div className="w-full max-w-(--container-width-md)">
                {agent ? (
                  <div className="neumo-raised flex flex-col items-center gap-4 rounded-2xl px-6 py-8 text-center">
                    <span className="neumo-inset-sm inline-flex h-14 w-14 items-center justify-center rounded-2xl text-[color:var(--neumo-success)]">
                      <CheckCircleIcon className="h-8 w-8" />
                    </span>
                    <p className="font-semibold text-[color:var(--neumo-text)]">
                      {t.agents.agentCreated}
                    </p>
                    <div className="flex flex-wrap justify-center gap-2">
                      <Button
                        variant="ghost"
                        className="neumo-btn-accent h-10 rounded-xl px-4 text-sm font-medium"
                        onClick={() =>
                          router.push(
                            `/workspace/agents/${agentName}/chats/new`,
                          )
                        }
                      >
                        {t.agents.startChatting}
                      </Button>
                      <Button
                        variant="ghost"
                        className="neumo-btn h-10 rounded-xl px-4 text-sm font-medium text-[color:var(--neumo-text)]"
                        onClick={() => router.push("/workspace/agents")}
                      >
                        {t.agents.backToGallery}
                      </Button>
                    </div>
                  </div>
                ) : (
                  <PromptInput
                    className="neumo-raised before:[content=''] relative z-10 rounded-3xl p-1.5 transition-all duration-300 ease-in-out before:pointer-events-none before:absolute before:inset-1.5 before:rounded-[1.4rem] before:bg-[color:var(--neumo-bg)] before:transition-shadow before:duration-300 before:ease-in-out focus-within:before:shadow-[inset_3px_3px_6px_#b8bcc2,inset_-3px_-3px_6px_#ffffff]"
                    disabled={thread.isLoading}
                    onSubmit={({ text }) => void handleChatSubmit(text)}
                  >
                    <PromptInputTextarea
                      autoFocus
                      placeholder={t.agents.createPageSubtitle}
                      disabled={thread.isLoading}
                      className="min-h-6! w-full min-w-0 px-4 py-2 leading-6! outline-none"
                    />
                    <PromptInputFooter className="justify-end px-2 pb-2">
                      <PromptInputSubmit
                        disabled={thread.isLoading}
                        className="neumo-btn-accent size-10 rounded-2xl text-white"
                      />
                    </PromptInputFooter>
                  </PromptInput>
                )}
              </div>
            </div>
          </main>
        </div>
      </ArtifactsProvider>
    </ThreadContext.Provider>
  );
}
