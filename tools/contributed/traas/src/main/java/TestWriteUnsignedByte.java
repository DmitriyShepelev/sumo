import de.uniluebeck.itm.tcpip.Storage;

public class TestWriteUnsignedByte {
    public static void main(String[] args) {
        Storage s = new Storage();
        Object[] o = new Object[] {256, 256, 256};
        s.writeUnsignedByte((byte) o[2]);
    }
}