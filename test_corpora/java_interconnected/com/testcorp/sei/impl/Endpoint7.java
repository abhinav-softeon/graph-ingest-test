package com.testcorp.sei.impl;

import javax.jws.WebMethod;
import javax.jws.WebService;
import com.testcorp.api.Handler;
import com.testcorp.api.HandlerImpl7;
import com.testcorp.manager.Manager7;
import com.testcorp.facade.Facade7;
import com.testcorp.service.Service7;

@WebService
public class Endpoint7 {

    @WebMethod
    public String lookup(String id) throws Exception {
        return new Service7().handle(id);
    }

    @WebMethod
    public String dispatch(String id) throws Exception {
        Handler h = new HandlerImpl7();
        return h.run(id);
    }

    @WebMethod
    public String viaManager(String id) throws Exception {
        return new Manager7().process(id);
    }

    @WebMethod
    public String deepChain(String id) throws Exception {
        return new Manager7().deep(id);
    }

    @WebMethod
    public String deepest(String id) throws Exception {
        return new Facade7().orchestrate(id);
    }

    @WebMethod
    public String viaFacade(String id) throws Exception {
        return new Facade7().orchestrateDirect(id);
    }
}
