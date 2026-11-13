import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// /playground 아래에서 서빙된다(같은 도메인·같은 세션 쿠키). 개발 시 vite(:5174) → FastAPI(:8501) 로 /api 프록시
export default defineConfig({
  base: "/playground/",
  plugins: [react()],
  server: { port: 5174, proxy: { "/api": "http://localhost:8501" } },
});
