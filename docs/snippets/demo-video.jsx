export const DemoVideo = ({ name, label }) => (
  <video
    controls
    preload="none"
    playsInline
    poster={`https://www.getcanonic.app/media/${name}_poster.jpg`}
    aria-label={label}
    style={{ width: "100%", aspectRatio: "16 / 9", borderRadius: "12px", background: "#000" }}
  >
    <source src={`https://www.getcanonic.app/media/${name}_web.mp4`} type="video/mp4" />
    <track kind="captions" src={`/media/${name}.vtt`} srcLang="en" label="English" />
  </video>
);
