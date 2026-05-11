import { createBrowserRouter } from 'react-router';

import { AppShell } from '@/app/shell';
import { NotFoundPage } from '@/app/views/not-found-page';
import { SessionRoomPage } from '@/features/sessions/routes/session-room-page';
import { SessionsHomePage } from '@/features/sessions/routes/sessions-home-page';

export const router = createBrowserRouter([
  {
    path: '/',
    element: <AppShell />,
    children: [
      {
        index: true,
        element: <SessionsHomePage />,
      },
      {
        path: 'sessions/:sessionId',
        element: <SessionRoomPage />,
      },
      {
        path: '*',
        element: <NotFoundPage />,
      },
    ],
  },
]);
