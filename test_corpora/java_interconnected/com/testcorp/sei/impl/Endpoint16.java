package com.testcorp.sei.impl;

import javax.jws.WebMethod;
import javax.jws.WebService;
import com.testcorp.api.Handler;
import com.testcorp.api.HandlerImpl4;
import com.testcorp.manager.Manager6;
import com.testcorp.facade.Facade0;
import com.testcorp.service.Service16;

@WebService
public class Endpoint16 {

    @WebMethod
    public String lookup(String id) throws Exception {
        return new Service16().handle(id);
    }

    @WebMethod
    public String dispatch(String id) throws Exception {
        Handler h = new HandlerImpl4();
        return h.run(id);
    }

    @WebMethod
    public String viaManager(String id) throws Exception {
        return new Manager6().process(id);
    }

    @WebMethod
    public String deepChain(String id) throws Exception {
        return new Manager6().deep(id);
    }

    @WebMethod
    public String deepest(String id) throws Exception {
        return new Facade0().orchestrate(id);
    }

    @WebMethod
    public String viaFacade(String id) throws Exception {
        return new Facade0().orchestrateDirect(id);
    }
}
