import { cn } from "@/lib/utils";

export function SettingsSection({
  className,
  title,
  description,
  children,
}: {
  className?: string;
  title: React.ReactNode;
  description?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section
      className={cn(
        "neumo-raised rounded-2xl p-6 transition-shadow duration-300 ease-in-out",
        className,
      )}
    >
      <header className="space-y-2">
        <h2 className="text-lg font-semibold text-[var(--neumo-text)]">
          {title}
        </h2>
        {description && (
          <p className="text-sm text-[var(--neumo-text-muted)]">
            {description}
          </p>
        )}
      </header>
      <main className="mt-4">{children}</main>
    </section>
  );
}
