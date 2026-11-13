import { createContext, useContext, useMemo, useState, type ReactNode } from 'react';

interface MessageState {
  message: string;
  setMessage: (message: string) => void;
}

const MessageContext = createContext<MessageState | null>(null);

export function MessageProvider({ children }: { children: ReactNode }) {
  const [message, setMessage] = useState('');
  const value = useMemo(() => ({ message, setMessage }), [message]);
  return <MessageContext.Provider value={value}>{children}</MessageContext.Provider>;
}

export function useMessage(): MessageState {
  const value = useContext(MessageContext);
  if (!value) throw new Error('useMessage must be used inside MessageProvider');
  return value;
}
