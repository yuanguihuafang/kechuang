"use client";

import {
  BookOpenTextIcon,
  CheckIcon,
  CopyIcon,
  ExternalLinkIcon,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import {
  formatCitationMarkdownReference,
  type CitationSource,
} from "@/core/citations/sources";
import { writeTextToClipboard } from "@/core/clipboard";
import { useI18n } from "@/core/i18n/hooks";
import { cn } from "@/lib/utils";

export function CitationSourcesPanel({
  className,
  sources,
}: {
  className?: string;
  sources: CitationSource[];
}) {
  const { t } = useI18n();

  if (sources.length === 0) {
    return null;
  }

  return (
    <details
      className={cn(
        "not-prose neumo-inset-sm mt-2 rounded-xl text-xs",
        className,
      )}
    >
      <summary className="text-muted-foreground hover:text-foreground flex cursor-pointer list-none items-center gap-2 px-3 py-2 transition-colors [&::-webkit-details-marker]:hidden">
        <BookOpenTextIcon className="size-3.5 shrink-0" />
        <span className="font-medium">
          {t.citations.sourcesSummary(sources.length)}
        </span>
      </summary>
      <ol className="max-h-80 divide-y divide-[var(--neumo-shadow)]/20 overflow-y-auto overscroll-contain">
        {sources.map((source, index) => (
          <li key={source.id} className="flex min-w-0 items-center gap-2 p-2">
            <span className="text-muted-foreground w-5 shrink-0 text-right tabular-nums">
              {index + 1}
            </span>
            <a
              href={source.url}
              target="_blank"
              rel="noopener noreferrer"
              className="flex min-w-0 flex-1 items-center gap-2 rounded px-2 py-1 transition-colors duration-200 ease-in-out hover:bg-[var(--neumo-bg-deep)]/20"
            >
              <span className="min-w-0 flex-1">
                <span className="text-foreground block truncate font-medium">
                  {source.title}
                </span>
                <span className="text-muted-foreground block truncate">
                  {source.domain}
                </span>
              </span>
              <span className="text-muted-foreground shrink-0">
                {t.citations.citeCount(source.count)}
              </span>
              <ExternalLinkIcon className="text-muted-foreground size-3.5 shrink-0" />
            </a>
            <CitationSourceCopyButton source={source} />
          </li>
        ))}
      </ol>
    </details>
  );
}

function CitationSourceCopyButton({ source }: { source: CitationSource }) {
  const { t } = useI18n();
  const [copied, setCopied] = useState(false);
  const resetTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const copyLabel = t.citations.copyReference(source.title);
  const copiedLabel = t.citations.copiedReference(source.title);

  useEffect(() => {
    return () => {
      if (resetTimerRef.current) {
        clearTimeout(resetTimerRef.current);
      }
    };
  }, []);

  const handleCopy = useCallback(() => {
    void (async () => {
      const didCopy = await writeTextToClipboard(
        formatCitationMarkdownReference(source),
      );
      if (!didCopy) {
        toast.error(t.clipboard.failedToCopyToClipboard);
        return;
      }

      setCopied(true);
      toast.success(t.clipboard.copiedToClipboard);
      if (resetTimerRef.current) {
        clearTimeout(resetTimerRef.current);
      }
      resetTimerRef.current = setTimeout(() => {
        setCopied(false);
        resetTimerRef.current = null;
      }, 2000);
    })().catch(() => {
      toast.error(t.clipboard.failedToCopyToClipboard);
    });
  }, [
    source,
    t.clipboard.copiedToClipboard,
    t.clipboard.failedToCopyToClipboard,
  ]);

  return (
    <button
      type="button"
      className="shrink-0 rounded p-1.5 text-[var(--neumo-text-muted)] transition-colors duration-200 ease-in-out hover:bg-[var(--neumo-bg-deep)]/20 hover:text-[var(--neumo-text)]"
      aria-label={copied ? copiedLabel : copyLabel}
      title={copied ? copiedLabel : copyLabel}
      data-copied-label={copiedLabel}
      onClick={handleCopy}
    >
      {copied ? (
        <CheckIcon className="size-3.5 text-green-500" />
      ) : (
        <CopyIcon className="size-3.5" />
      )}
    </button>
  );
}
