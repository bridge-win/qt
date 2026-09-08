import "@ant-design/v5-patch-for-react-19";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createRoot } from "react-dom/client";
import WorkbenchApp from "./App";
import "./styles.css";

const client = new QueryClient({
  defaultOptions: {
    queries: { retry: (attempt, error) => !(error instanceof Error && /401|403|404/.test(error.message)) && attempt < 2, refetchOnWindowFocus: false },
    mutations: { retry: false },
  },
});

createRoot(document.getElementById("root")!).render(<QueryClientProvider client={client}><WorkbenchApp /></QueryClientProvider>);
