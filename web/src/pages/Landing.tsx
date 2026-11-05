import { BrandMark } from "../components/Icons";
import { LandingPreview } from "../components/LandingPreview";

interface LandingProps {
  onOpenWorkbench: () => void;
}

const REPO_URL = "https://github.com/JHf0912/scanned-pdf-qa-agent";

export function Landing({ onOpenWorkbench }: LandingProps) {
  return (
    <div className="landing">
      <header className="landing-nav">
        <div className="nav-inner">
          <a className="brand" href="/" onClick={(event) => event.preventDefault()}>
            <BrandMark size={26} />
            <span className="brand-name">docqa</span>
          </a>
          <nav className="nav-links" aria-label="主导航">
            <a href="#why">为什么</a>
            <a href="#forms">运行形态</a>
            <a href={REPO_URL} target="_blank" rel="noreferrer">
              源码
            </a>
          </nav>
          <button type="button" className="btn btn-primary nav-cta" onClick={onOpenWorkbench}>
            打开工作台
          </button>
        </div>
      </header>

      <main>
        <section className="hero" aria-labelledby="hero-title">
          <div className="hero-copy">
            <div className="hero-eyebrow">
              <span className="eyebrow-dot" aria-hidden="true" />
              PDF 与 DOCX · 有据问答
            </div>
            <h1 id="hero-title" className="hero-title">
              让文档里的每一句话，
              <span className="mark">都能被引用和核对</span>。
            </h1>
            <p className="hero-sub">
              上传没有文本层的 PDF 或原生 DOCX，提问，得到带页码/段落号与条款号的答案。
              每一项自检、每一次拒答，都告诉你为什么。
            </p>
            <div className="hero-actions">
              <button type="button" className="btn btn-primary btn-lg" onClick={onOpenWorkbench}>
                打开工作台
                <span aria-hidden="true">→</span>
              </button>
              <a className="btn btn-lg" href={REPO_URL} target="_blank" rel="noreferrer">
                查看源码
              </a>
            </div>
            <ul className="hero-points" aria-label="核心特征">
              <li>引用可定位</li>
              <li>自检逐项可见</li>
              <li>六类可解释拒答</li>
            </ul>
            <p className="hero-note">
              本地运行，默认只监听 <span className="mono">127.0.0.1</span>；也可用 Docker 部署。
            </p>
          </div>
          <LandingPreview />
        </section>

        <section className="landing-section" id="why" aria-labelledby="why-title">
          <div className="section-head">
            <span className="section-kicker">为什么不是又一个 RAG Demo</span>
            <h2 id="why-title" className="section-title">
              答案之外，把“凭什么”也摆出来。
            </h2>
          </div>
          <div className="value-grid">
            <article className="value-item">
              <span className="value-num">01</span>
              <h3>引用不是装饰</h3>
              <p>
                PDF 的结论带 <span className="mono">[p.3 §3.6]</span>，DOCX 带{" "}
                <span className="mono">[§3.6 ¶4]</span>；点开就是命中的原文片段。
                没有条号时退化为段落号，短公文也不会因为强制条号而崩掉。
              </p>
            </article>
            <article className="value-item">
              <span className="value-num">02</span>
              <h3>自检不是一句“已检查”</h3>
              <p>
                引用有效性、数值落地、条号一致性逐项列出。不过门会触发修复或拒答，
                而不是在报告里写一句通过。
              </p>
            </article>
            <article className="value-item">
              <span className="value-num">03</span>
              <h3>拒答不是“无法回答”</h3>
              <p>
                不在文档、识别失败、问题歧义、自检不过、超出范围、低置信度——
                六类原因，对应六种下一步动作。
              </p>
            </article>
          </div>
        </section>

        <section className="landing-section forms" id="forms" aria-labelledby="forms-title">
          <div className="section-head">
            <span className="section-kicker">运行形态</span>
            <h2 id="forms-title" className="section-title">
              同一套后端，四种用法。
            </h2>
          </div>
          <div className="form-strip">
            <div className="form-item">
              <span className="form-tag">CLI</span>
              <code>uv run docqa ask "…"</code>
              <p>批处理、复现实验、CI 的主路径。</p>
            </div>
            <div className="form-item">
              <span className="form-tag">Web</span>
              <code>uv run docqa serve</code>
              <p>本机工作台，支持上传 PDF / DOCX、切换文档、看引用与自检。</p>
            </div>
            <div className="form-item">
              <span className="form-tag">Report</span>
              <code>docqa eval --report …</code>
              <p>静态 HTML，不需要服务器或密钥就能打开。</p>
            </div>
            <div className="form-item">
              <span className="form-tag">Docker</span>
              <code>docker run -p 8000:8000 …</code>
              <p>把同一个 Web UI 部署到能访问的机器上。</p>
            </div>
          </div>
        </section>

        <section className="landing-cta" aria-labelledby="cta-title">
          <div>
            <h2 id="cta-title">先上传一份文档，问一个具体问题。</h2>
            <p>答案会带引用；证据不足时，它会告诉你缺在哪。</p>
          </div>
          <button type="button" className="btn btn-primary btn-lg" onClick={onOpenWorkbench}>
            打开工作台
            <span aria-hidden="true">→</span>
          </button>
        </section>
      </main>

      <footer className="landing-footer">
        <span>docqa · PDF / DOCX 智能文档问答</span>
        <span>
          自身代码 MIT · 依赖 PyMuPDF AGPL-3.0 ·{" "}
          <a href={REPO_URL} target="_blank" rel="noreferrer">
            GitHub
          </a>
        </span>
      </footer>
    </div>
  );
}
