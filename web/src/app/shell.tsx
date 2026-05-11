import { Outlet } from 'react-router';

export function AppShell() {
  return (
    <div className="min-h-screen bg-[var(--background)] text-[var(--foreground)]">
      <main className="mx-auto min-h-screen w-full max-w-[1040px] px-5 py-10 sm:px-8">
        <Outlet />
      </main>
    </div>
  );
}
