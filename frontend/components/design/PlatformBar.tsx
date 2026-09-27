const links = [
  { label: "穿搭", href: process.env.NEXT_PUBLIC_LINK_OUTFIT || "http://localhost:3010" },
  { label: "产品", href: process.env.NEXT_PUBLIC_LINK_PRODUCT || "http://localhost:3000" },
  { label: "设计", href: process.env.NEXT_PUBLIC_LINK_DESIGN || "http://localhost:5180", current: true },
  { label: "CMS", href: process.env.NEXT_PUBLIC_LINK_CMS || "http://localhost:8001" },
];

export default function PlatformBar() {
  return (
    <header className="platformBar">
      <div className="platformBrand">
        <b>鞋服 AI 协作平台</b>
        <i aria-hidden="true" />
        <span>鞋服设计</span>
      </div>
      <nav className="platformNav" aria-label="协作平台">
        {links.map((link, index) => (
          <span key={link.label}>
            {index > 0 && <span aria-hidden="true"> · </span>}
            <a href={link.href} aria-current={link.current ? "page" : undefined}>{link.label}</a>
          </span>
        ))}
      </nav>
    </header>
  );
}
