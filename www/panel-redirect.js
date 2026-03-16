// source: https://gist.github.com/balloob/580deaf8c3fc76948559c5963ed4d436

class PanelRedirect extends HTMLElement {
  connectedCallback() {
    if (this._info) {
      this._navigate();
    }
  }

  set panel(info) {
    this._info = info;

    if (this.isConnected) {
      this._navigate();
    }
  }

  _navigate() {
    history.replaceState(null, "", this._info.config.target);
    const event = new Event("location-changed", {
      bubbles: true,
      composed: true,
    });
    event.detail = { replace: true };
    this.dispatchEvent(event);
  }
}

customElements.define("panel-redirect", PanelRedirect);