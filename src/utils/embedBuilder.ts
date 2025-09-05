import { EmbedBuilder } from "discord.js";

class CustomEmbed extends EmbedBuilder {
  constructor() {
    super();
    this.setColor("#f0d669");
  }

  setRed() {
    this.setColor("#ff0000");

    return this;
  }
}

export { CustomEmbed };
