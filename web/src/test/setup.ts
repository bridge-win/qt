import "@testing-library/jest-dom/vitest";

Object.defineProperty(window, "matchMedia", { value: () => ({ matches: false, addListener: () => undefined, removeListener: () => undefined, addEventListener: () => undefined, removeEventListener: () => undefined }) });

const getComputedStyle = window.getComputedStyle.bind(window);
Object.defineProperty(window, "getComputedStyle", { value: (element: Element) => getComputedStyle(element) });
