// kordoc 이 createRequire(import.meta.url)("cfb") 로 런타임에 부르는 패키지를 번들 안의 사본으로 돌려준다.
import { createRequire as nodeCreateRequire } from "node:module"
import cfb from "cfb"
const BUNDLED = { cfb }
export function createRequire(url) {
  const req = nodeCreateRequire(url)
  return id => (id in BUNDLED ? BUNDLED[id] : req(id))
}
export default { createRequire }
